//! This module contains the `concolic` stages, which can trace a target using symbolic execution
//! and use the results for fuzzer input and mutations.
//!
use alloc::borrow::{Cow, ToOwned};
#[cfg(feature = "concolic_mutation")]
use alloc::{string::ToString, vec::Vec};
#[cfg(feature = "concolic_mutation")]
use core::marker::PhantomData;
use hashbrown::HashMap;
use serde::{de::value::StringDeserializer, Deserialize, Serialize};
#[cfg(feature = "std")]
use std::string::String;

use libafl_bolts::{
    tuples::{Handle, MatchNameRef},
    Named,
};

#[cfg(all(feature = "concolic_mutation", feature = "introspection"))]
use crate::monitors::PerfFeature;
#[cfg(all(feature = "introspection", feature = "concolic_mutation"))]
use crate::state::HasClientPerfMonitor;
use crate::{
    executors::{Executor, HasObservers},
    inputs::Input,
    observers::concolic::ConcolicObserver,
    stages::{RetryCountRestartHelper, Stage, TracingStage},
    state::{HasCorpus, HasCurrentTestcase, HasExecutions, UsesState},
    Error, HasMetadata, HasNamedMetadata,
};
#[cfg(feature = "concolic_mutation")]
use crate::{
    inputs::HasMutatorBytes,
    mark_feature_time,
    observers::concolic::{ConcolicMetadata, SymExpr, SymExprRef},
    start_timer,
    state::State,
    Evaluator,
};

/// Wraps a [`TracingStage`] to add concolic observing.
#[derive(Clone, Debug)]
pub struct ConcolicTracingStage<'a, EM, TE, Z> {
    name: Cow<'static, str>,
    inner: TracingStage<EM, TE, Z>,
    observer_handle: Handle<ConcolicObserver<'a>>,
}

impl<EM, TE, Z> UsesState for ConcolicTracingStage<'_, EM, TE, Z>
where
    TE: UsesState,
{
    type State = TE::State;
}

/// The name for concolic tracer
pub const CONCOLIC_TRACING_STAGE_NAME: &str = "concolictracing";

impl<EM, TE, Z> Named for ConcolicTracingStage<'_, EM, TE, Z> {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<E, EM, TE, Z> Stage<E, EM, Z> for ConcolicTracingStage<'_, EM, TE, Z>
where
    E: UsesState<State = Self::State>,
    EM: UsesState<State = Self::State>,
    TE: Executor<EM, Z> + HasObservers,
    Self::State: HasExecutions + HasCorpus + HasNamedMetadata,
    Z: UsesState<State = Self::State>,
{
    #[inline]
    fn perform(
        &mut self,
        fuzzer: &mut Z,
        _executor: &mut E,
        state: &mut Self::State,
        manager: &mut EM,
    ) -> Result<(), Error> {
        self.inner.trace(fuzzer, state, manager)?;
        if let Some(observer) = self.inner.executor().observers().get(&self.observer_handle) {
            let metadata = observer.create_metadata_from_current_map();
            state
                .current_testcase_mut()?
                .metadata_map_mut()
                .insert(metadata);
        }
        Ok(())
    }

    fn should_restart(&mut self, state: &mut Self::State) -> Result<bool, Error> {
        // This is a deterministic stage
        // Once it failed, then don't retry,
        // It will just fail again
        RetryCountRestartHelper::no_retry(state, &self.name)
    }

    fn clear_progress(&mut self, state: &mut Self::State) -> Result<(), Error> {
        RetryCountRestartHelper::clear_progress(state, &self.name)
    }
}

impl<'a, EM, TE, Z> ConcolicTracingStage<'a, EM, TE, Z> {
    /// Creates a new default tracing stage using the given [`Executor`], observing traces from a
    /// [`ConcolicObserver`] with the given name.
    pub fn new(
        inner: TracingStage<EM, TE, Z>,
        observer_handle: Handle<ConcolicObserver<'a>>,
    ) -> Self {
        let observer_name = observer_handle.name().clone();
        Self {
            inner,
            observer_handle,
            name: Cow::Owned(
                CONCOLIC_TRACING_STAGE_NAME.to_owned() + ":" + observer_name.into_owned().as_str(),
            ),
        }
    }
}

#[cfg(feature = "concolic_mutation")]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StringReplacement {
    pub begin: usize,
    pub end: usize,
    pub nonce: u64,
    pub value: String,
}

#[cfg(feature = "concolic_mutation")]
/// The maximum number of arguments that can be extracted from a scanf call.
/// Defined in symcc/runtime/LibcWrappers.cpp
const SCANF_MAX_ARG_CNT: u64 = 7;

#[cfg(feature = "concolic_mutation")]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SatQuery {
    // Whether the branch specified in site_id was taken during execution. Because we're attempting
    // to invert this branch outcome, the solution will result in this branch being taken if taken
    // == false and vice versa
    pub taken: bool,
    pub site_id: usize,
    pub assertions: Vec<String>,
    pub solution_index: usize,
}
#[cfg(feature = "concolic_mutation")]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UnsatQuery {
    pub taken: bool,
    pub site_id: usize,
    pub assertions: Vec<String>,
}

/// Mutation result from solving SMT constraints
#[cfg(feature = "concolic_mutation")]
#[derive(Debug, Clone)]
pub struct ConcolicMutationResultTotal {
    pub mutations: Vec<ConcolicMutationResult>,
    pub unsat_queries: Vec<UnsatQuery>,
    pub sat_queries: Vec<SatQuery>,
}

#[cfg(feature = "concolic_mutation")]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConcolicMutationResult {
    pub byte_replacements: Vec<(usize, u8)>,
    pub string_replacements: Vec<StringReplacement>,
}

struct ScanfArgInfo {
    input_range: (usize, usize),
    format_string: String,
    // The bool denotes whether the scanf for that argument was successful
    args: HashMap<u64, (bool, u64)>,
}

/// Generate mutations from a concolic trace
#[cfg(feature = "concolic_mutation")]
#[allow(clippy::too_many_lines)]
pub fn generate_mutations(
    iter: impl Iterator<Item = (SymExprRef, SymExpr)>,
) -> ConcolicMutationResultTotal {
    use z3::{
        ast::{Ast, Bool, Dynamic, BV},
        Config, Context, Solver, Symbol,
    };
    fn build_extract<'ctx>(
        bv: &BV<'ctx>,
        offset: u64,
        length: u64,
        little_endian: bool,
    ) -> BV<'ctx> {
        let size = u64::from(bv.get_size());
        assert_eq!(
            size % 8,
            0,
            "can't extract on byte-boundary on BV that is not byte-sized"
        );

        if little_endian {
            (0..length)
                .map(|i| {
                    bv.extract(
                        (size - (offset + i) * 8 - 1).try_into().unwrap(),
                        (size - (offset + i + 1) * 8).try_into().unwrap(),
                    )
                })
                .reduce(|acc, next| next.concat(&acc))
                .unwrap()
        } else {
            bv.extract(
                (size - offset * 8 - 1).try_into().unwrap(),
                (size - (offset + length) * 8).try_into().unwrap(),
            )
        }
    }

    let mut res = Vec::new();
    let mut sat_queries = Vec::new();
    let mut unsat_queries = Vec::new();

    let mut cfg = Config::new();
    cfg.set_timeout_msec(10_000);
    let ctx = Context::new(&cfg);
    let solver = Solver::new(&ctx);

    let mut translation = HashMap::<SymExprRef, Dynamic>::new();

    macro_rules! bool {
        ($op:ident) => {
            translation[&$op].as_bool().unwrap()
        };
    }

    macro_rules! bv {
        ($op:ident) => {
            translation[&$op].as_bv().unwrap()
        };
    }

    macro_rules! bv_binop {
        ($a:ident $op:tt $b:ident) => {
            Some(bv!($a).$op(&bv!($b)).into())
        };
    }

    // We maintain a default value of zero for all scanf-extracts
    // This is because we can't guarantee that all scanf-extracts will be referenced
    // in the model output
    // This happens if the target program checks the return value of scanf and exits upon
    // scanf failure. In this case, the scanf-extract will exist in the trace but
    // will not be referenced in the model output
    // mapping from scanf-extract nonce => [arg_idx]
    let mut scanf_extracts: HashMap<u64, Vec<SymExpr>> = HashMap::new();
    for (id, msg) in iter {
        let z3_expr: Option<Dynamic> = match msg {
            SymExpr::InputByte { offset, .. } => {
                Some(BV::new_const(&ctx, Symbol::Int(offset as u32), 8).into())
            }
            SymExpr::ScanfExtract {
                ref format_string,
                input_begin,
                input_end,
                arg_idx,
                arg_size,
                nonce,
                success,
            } => {
                let expr: Dynamic = BV::new_const(
                    &ctx,
                    // although not descriptive enough, easier to parse
                    Symbol::String(format!("Scanf({},{})", nonce, arg_idx)),
                    (arg_size as u32) * 8,
                )
                .into();
                scanf_extracts
                    .entry(nonce)
                    .or_insert_with(Vec::new)
                    .push(msg.clone());
                Some(expr)
            }
            SymExpr::Integer { value, bits } => {
                Some(BV::from_u64(&ctx, value, u32::from(bits)).into())
            }
            SymExpr::Integer128 { high: _, low: _ } => todo!(),
            SymExpr::IntegerFromBuffer {} => todo!(),
            SymExpr::NullPointer => Some(BV::from_u64(&ctx, 0, usize::BITS).into()),
            SymExpr::True => Some(Bool::from_bool(&ctx, true).into()),
            SymExpr::False => Some(Bool::from_bool(&ctx, false).into()),
            SymExpr::Bool { value } => Some(Bool::from_bool(&ctx, value).into()),
            SymExpr::Neg { op } => Some(bv!(op).bvneg().into()),
            SymExpr::Add { a, b } => bv_binop!(a bvadd b),
            SymExpr::Sub { a, b } => bv_binop!(a bvsub b),
            SymExpr::Mul { a, b } => bv_binop!(a bvmul b),
            SymExpr::UnsignedDiv { a, b } => bv_binop!(a bvudiv b),
            SymExpr::SignedDiv { a, b } => bv_binop!(a bvsdiv b),
            SymExpr::UnsignedRem { a, b } => bv_binop!(a bvurem b),
            SymExpr::SignedRem { a, b } => bv_binop!(a bvsrem b),
            SymExpr::ShiftLeft { a, b } => bv_binop!(a bvshl b),
            SymExpr::LogicalShiftRight { a, b } => bv_binop!(a bvlshr b),
            SymExpr::ArithmeticShiftRight { a, b } => bv_binop!(a bvashr b),
            SymExpr::SignedLessThan { a, b } => bv_binop!(a bvslt b),
            SymExpr::SignedLessEqual { a, b } => bv_binop!(a bvsle b),
            SymExpr::SignedGreaterThan { a, b } => bv_binop!(a bvsgt b),
            SymExpr::SignedGreaterEqual { a, b } => bv_binop!(a bvsge b),
            SymExpr::UnsignedLessThan { a, b } => bv_binop!(a bvult b),
            SymExpr::UnsignedLessEqual { a, b } => bv_binop!(a bvule b),
            SymExpr::UnsignedGreaterThan { a, b } => bv_binop!(a bvugt b),
            SymExpr::UnsignedGreaterEqual { a, b } => bv_binop!(a bvuge b),
            SymExpr::Not { op } => {
                let translated = &translation[&op];
                Some(if let Some(bv) = translated.as_bv() {
                    bv.bvnot().into()
                } else if let Some(bool) = translated.as_bool() {
                    bool.not().into()
                } else {
                    panic!(
                        "unexpected z3 expr of type {:?} when applying not operation",
                        translated.kind()
                    )
                })
            }
            SymExpr::Equal { a, b } => Some(translation[&a]._eq(&translation[&b]).into()),
            SymExpr::NotEqual { a, b } => Some(translation[&a]._eq(&translation[&b]).not().into()),
            SymExpr::BoolAnd { a, b } => Some(Bool::and(&ctx, &[&bool!(a), &bool!(b)]).into()),
            SymExpr::BoolOr { a, b } => Some(Bool::or(&ctx, &[&bool!(a), &bool!(b)]).into()),
            SymExpr::BoolXor { a, b } => Some(bool!(a).xor(&bool!(b)).into()),
            SymExpr::And { a, b } => bv_binop!(a bvand b),
            SymExpr::Or { a, b } => bv_binop!(a bvor b),
            SymExpr::Xor { a, b } => bv_binop!(a bvxor b),
            SymExpr::Sext { op, bits } => Some(bv!(op).sign_ext(u32::from(bits)).into()),
            SymExpr::Zext { op, bits } => Some(bv!(op).zero_ext(u32::from(bits)).into()),
            SymExpr::Trunc { op, bits } => Some(bv!(op).extract(u32::from(bits - 1), 0).into()),
            SymExpr::BoolToBit { op } => Some(
                bool!(op)
                    .ite(&BV::from_u64(&ctx, 1, 1), &BV::from_u64(&ctx, 0, 1))
                    .into(),
            ),
            SymExpr::Concat { a, b } => bv_binop!(a concat b),
            SymExpr::Extract {
                op,
                first_bit,
                last_bit,
            } => Some(bv!(op).extract(first_bit as u32, last_bit as u32).into()),
            SymExpr::Insert {
                target,
                to_insert,
                offset,
                little_endian,
            } => {
                let target = bv!(target);
                let to_insert = bv!(to_insert);
                let bits_to_insert = u64::from(to_insert.get_size());
                assert_eq!(bits_to_insert % 8, 0, "can only insert full bytes");
                let after_len = (u64::from(target.get_size()) / 8) - offset - (bits_to_insert / 8);
                Some(
                    [
                        if offset == 0 {
                            None
                        } else {
                            Some(build_extract(&target, 0, offset, false))
                        },
                        Some(if little_endian {
                            build_extract(&to_insert, 0, bits_to_insert / 8, true)
                        } else {
                            to_insert
                        }),
                        if after_len == 0 {
                            None
                        } else {
                            Some(build_extract(
                                &target,
                                offset + (bits_to_insert / 8),
                                after_len,
                                false,
                            ))
                        },
                    ]
                    .into_iter()
                    .reduce(|acc: Option<BV>, val: Option<BV>| match (acc, val) {
                        (Some(prev), Some(next)) => Some(prev.concat(&next)),
                        (Some(prev), None) => Some(prev),
                        (None, next) => next,
                    })
                    .unwrap()
                    .unwrap()
                    .into(),
                )
            }
            _ => None,
        };
        if let Some(expr) = z3_expr {
            translation.insert(id, expr);
        } else if let SymExpr::PathConstraint {
            constraint,
            taken,
            location: _,
        } = msg
        {
            let op = translation[&constraint].as_bool().unwrap();
            let op = if taken { op } else { op.not() }.simplify();
            if op.as_bool().is_none() {
                let negated_constraint = op.not().simplify();
                solver.push();
                solver.assert(&negated_constraint);
                match solver.check() {
                    z3::SatResult::Unsat => {
                        // negation is unsat => no mutation
                        let assertions = solver.get_assertions();
                        unsat_queries.push(UnsatQuery {
                            taken,
                            site_id: id.into(),
                            assertions: assertions.iter().map(|x| x.to_string()).collect(),
                        });
                        solver.pop(1);
                        // check that out path is ever still sat, otherwise, we can stop trying
                        if matches!(
                            solver.check(),
                            z3::SatResult::Unknown | z3::SatResult::Unsat
                        ) {
                            return ConcolicMutationResultTotal {
                                mutations: res,
                                unsat_queries,
                                sat_queries, 
                            };
                        }
                    }
                    z3::SatResult::Unknown => {
                        // we've got a problem. ignore
                    }
                    z3::SatResult::Sat => {
                        let model = solver.get_model().unwrap();
                        let model_string = model.to_string();
                        let mut byte_replacements = Vec::new();
                        // mapping from nonce -> ScanfArgInfo
                        let mut scanf_args: HashMap<u64, ScanfArgInfo> = HashMap::new();
                        for l in model_string.lines() {
                            if let [offset_str, value_str] =
                                l.split(" -> ").collect::<Vec<_>>().as_slice()
                            {
                                // byte level replacement
                                if let Ok(offset) =
                                    offset_str.trim_start_matches("k!").parse::<usize>()
                                {
                                    let value =
                                        u8::from_str_radix(value_str.trim_start_matches("#x"), 16)
                                            .unwrap();
                                    byte_replacements.push((offset, value));
                                }
                                // string level replacement via scanf-extract
                                else {
                                    // check if Scanf(..)
                                    if offset_str.starts_with("Scanf(") && offset_str.ends_with(")")
                                    {
                                        let description = offset_str
                                            .trim_start_matches("Scanf(")
                                            .trim_end_matches(")")
                                            .split(',')
                                            .map(|s| s.trim())
                                            .collect::<Vec<_>>();
                                        let nonce = description[0].parse::<u64>().unwrap();
                                        let arg_idx = description[1].parse::<u64>().unwrap();
                                        let arg_value = u64::from_str_radix(
                                            value_str.trim_start_matches("#x"),
                                            16,
                                        )
                                        .unwrap();

                                        if let SymExpr::ScanfExtract {
                                            format_string,
                                            input_begin,
                                            input_end,
                                            success,
                                            arg_idx,
                                            ..
                                        } = &scanf_extracts[&nonce][arg_idx as usize]
                                        {
                                            scanf_args
                                                .entry(nonce)
                                                .or_insert_with(|| ScanfArgInfo {
                                                    input_range: (
                                                        *input_begin as usize,
                                                        *input_end as usize,
                                                    ),
                                                    format_string: format_string.to_string(),
                                                    args: HashMap::new(),
                                                })
                                                .args
                                                .insert(*arg_idx, (*success, arg_value));
                                        } else {
                                            unreachable!();
                                        }
                                    } else {
                                        panic!("unexpected model output: {}", l);
                                    }
                                }
                            } else {
                                panic!();
                            }
                        }
                        let string_replacements: Vec<StringReplacement> = scanf_args
                            .iter()
                            .map(|(&nonce, info)| {
                                let replacement_string =
                                    construct_replacement_string(&info.format_string, &info.args);
                                StringReplacement {
                                    begin: info.input_range.0,
                                    end: info.input_range.1,
                                    nonce,
                                    value: replacement_string,
                                }
                            })
                            .collect();
                        let assertions = solver.get_assertions();
                        sat_queries.push(SatQuery{
                            taken,
                            site_id: id.into(),
                            assertions: assertions.iter().map(|x| x.to_string()).collect(),
                            solution_index: res.len(),
                
                        });
                        res.push(ConcolicMutationResult {
                            byte_replacements,
                            string_replacements,
                        });
                        solver.pop(1);
                    }
                };
                // assert the path constraint
                solver.assert(&op);
            }
        }
    }

    ConcolicMutationResultTotal {
        mutations: res,
        unsat_queries,
        sat_queries,
    } 
}

/// A mutational stage that uses Z3 to solve concolic constraints attached to the [`crate::corpus::Testcase`] by the [`ConcolicTracingStage`].
#[cfg(feature = "concolic_mutation")]
#[derive(Clone, Debug, Default)]
pub struct SimpleConcolicMutationalStage<Z> {
    name: Cow<'static, str>,
    phantom: PhantomData<Z>,
}

#[cfg(feature = "concolic_mutation")]
impl<Z> UsesState for SimpleConcolicMutationalStage<Z>
where
    Z: UsesState,
{
    type State = Z::State;
}

#[cfg(feature = "concolic_mutation")]
/// The unique id for this stage
static mut SIMPLE_CONCOLIC_MUTATIONAL_ID: usize = 0;

#[cfg(feature = "concolic_mutation")]
/// The name for concolic mutation stage
pub const SIMPLE_CONCOLIC_MUTATIONAL_NAME: &str = "concolicmutation";

#[cfg(feature = "concolic_mutation")]
impl<Z> Named for SimpleConcolicMutationalStage<Z> {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

/// Given a ScanfReplacement, construct a replacement string that produces the
/// extraction specified in the replacement.
#[cfg(feature = "concolic_mutation")]
pub fn construct_replacement_string(
    format_string: &str,
    args: &HashMap<u64, (bool, u64)>,
) -> String {
    use std::ffi::{CStr, CString};
    use std::ptr;

    let (mmap_page, addr) = unsafe {
        let mmap_page = libc::mmap(
            ptr::null_mut(),
            4096,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_PRIVATE | libc::MAP_ANON,
            -1,
            0,
        );
        if mmap_page == libc::MAP_FAILED {
            panic!("mmap failed");
        }
        // write "AAAA" to addr + 0x41
        let template_string = CString::new("XXXX").unwrap();
        let addr = (mmap_page as u64 + 0x41) as *mut libc::c_void;
        libc::memcpy(addr, template_string.as_ptr() as *const libc::c_void, 4);
        (mmap_page, addr)
    };

    let devnull_fp = unsafe {
        let fp = libc::fopen(
            CString::new("/dev/null").unwrap().as_ptr(),
            CString::new("w").unwrap().as_ptr(),
        );
        if fp.is_null() {
            panic!("fopen failed");
        }
        fp
    };

    let format_str = CString::new(format_string.clone()).unwrap();
    let format_str_ptr = format_str.as_ptr();
    let mut args_v = vec![];
    let mut total_success = true;
    for idx in 0u64..SCANF_MAX_ARG_CNT {
        if let Some((success, value)) = args.get(&idx) {
            // the entire scanf is unsuccessful if any of the arguments are unsuccessful
            total_success &= *success;
            args_v.push(*value);
        } else {
            // add 0x41 so that %c prints 'A' instead of '\0'
            args_v.push(addr as u64);
        }
    }
    // we support up to 7 variadic arguments
    let out = unsafe {
        let nbytes = libc::fprintf(
            devnull_fp,
            format_str_ptr,
            args_v[0],
            args_v[1],
            args_v[2],
            args_v[3],
            args_v[4],
            args_v[5],
            args_v[6],
        );
        libc::fclose(devnull_fp);
        let buffer = libc::malloc(nbytes as usize + 10);
        libc::sprintf(
            buffer as *mut i8,
            format_str_ptr,
            args_v[0],
            args_v[1],
            args_v[2],
            args_v[3],
            args_v[4],
            args_v[5],
            args_v[6],
        );
        // convert buffer to string
        let mut out = CStr::from_ptr(buffer as *mut i8)
            .to_str()
            .unwrap()
            .to_string();
        if !total_success {
            out.push_str("\n");
        }
        // Prepend a line break to the output. This is necessary for cases where
        // the developer does something like scanf("%d"); scanf("%d").
        // In this case, the \n (or space) from the first scanf will be passed on to the second scanf
        // It is okay to do so because
        // scanf will ignore leading line break
        out.insert_str(0, "\n");
        libc::munmap(mmap_page, 4096);
        libc::free(buffer);
        out
    };
    out
}

#[cfg(feature = "concolic_mutation")]
/// Produces new inputs from the given input and mutations
pub fn create_new_inputs<I: Input + HasMutatorBytes>(
    input: I,
    mutations: Vec<ConcolicMutationResult>,
) -> Vec<I> {
    let mut new_inputs = vec![];
    for mutation in mutations {
        let mut input_copy = input.clone();
        // First, apply byte replacements only
        for (index, new_byte) in mutation.byte_replacements {
            if index > input_copy.bytes().len() {
                continue;
            }
            input_copy.bytes_mut()[index] = new_byte;
        }
        new_inputs.push(input_copy.clone());

        // Then, apply scanf replacements. This has the effect of 'overwriting' any byte
        // replacement effects.
        let mut string_replacements = mutation.string_replacements;
        string_replacements.sort_by(|a, b| {
            let a_tuple = (a.begin, a.nonce);
            let b_tuple = (b.begin, b.nonce);
            a_tuple.cmp(&b_tuple)
        });
        let mut cursor = 0;
        let mut new_input_parts = vec![];
        for replacement in string_replacements {
            let begin = replacement.begin;
            let end = replacement.end;
            if cursor < begin {
                // copy cursor .. begin from original input
                new_input_parts.push(input_copy.bytes()[cursor..begin].to_vec());
            }
            // copy begin..end from replacement_string
            // this is for people who don't know how to use scanf (e.g. "%d", and not "%d\n",
            // btw, scanf("%d") is incorrect! always use scanf("%d\n")
            // causing the \n to be passed on to the subsequent stdin reads)
            new_input_parts.push(replacement.value.as_bytes().to_vec());
            cursor = end;
        }
        if cursor < input_copy.bytes().len() {
            // copy cursor .. end from original input
            new_input_parts.push(input_copy.bytes()[cursor..].to_vec());
        }
        let new_input_bytes = new_input_parts.into_iter().flatten().collect::<Vec<_>>();
        let mut new_input = input_copy.clone();
        new_input.resize(new_input_bytes.len(), 0);
        for (i, &byte) in new_input_bytes.iter().enumerate() {
            new_input.bytes_mut()[i] = byte;
        }
        new_inputs.push(new_input.clone());
    }
    new_inputs
}

#[cfg(feature = "concolic_mutation")]
impl<E, EM, Z> Stage<E, EM, Z> for SimpleConcolicMutationalStage<Z>
where
    E: UsesState<State = Self::State>,
    EM: UsesState<State = Self::State>,
    Z: Evaluator<E, EM>,
    Z::Input: HasMutatorBytes,
    Self::State: State + HasExecutions + HasCorpus + HasMetadata + HasNamedMetadata,
{
    #[inline]
    fn perform(
        &mut self,
        fuzzer: &mut Z,
        executor: &mut E,
        state: &mut Self::State,
        manager: &mut EM,
    ) -> Result<(), Error> {
        {
            start_timer!(state);
            mark_feature_time!(state, PerfFeature::GetInputFromCorpus);
        }
        let testcase = state.current_testcase()?.clone();

        let out = testcase.metadata::<ConcolicMetadata>().ok().map(|meta| {
            start_timer!(state);
            let mutations = { generate_mutations(meta.iter_messages()) };
            mark_feature_time!(state, PerfFeature::Mutate);
            mutations
        });

        if let Some(out) = out {
            let input_copy = state.current_input_cloned()?;
            let new_inputs = create_new_inputs(input_copy, out.mutations);
            // Time is measured directly the `evaluate_input` function
            for new_input in new_inputs {
                fuzzer.evaluate_input(state, executor, manager, new_input)?;
            }
        }
        Ok(())
    }

    #[inline]
    fn should_restart(&mut self, state: &mut Self::State) -> Result<bool, Error> {
        // This is a deterministic stage
        // Once it failed, then don't retry,
        // It will just fail again
        RetryCountRestartHelper::no_retry(state, &self.name)
    }

    #[inline]
    fn clear_progress(&mut self, state: &mut Self::State) -> Result<(), Error> {
        RetryCountRestartHelper::clear_progress(state, &self.name)
    }
}

#[cfg(feature = "concolic_mutation")]
impl<Z> SimpleConcolicMutationalStage<Z> {
    #[must_use]
    /// Construct this stage
    pub fn new() -> Self {
        // unsafe but impossible that you create two threads both instantiating this instance
        let stage_id = unsafe {
            let ret = SIMPLE_CONCOLIC_MUTATIONAL_ID;
            SIMPLE_CONCOLIC_MUTATIONAL_ID += 1;
            ret
        };
        Self {
            name: Cow::Owned(
                SIMPLE_CONCOLIC_MUTATIONAL_NAME.to_owned() + ":" + stage_id.to_string().as_str(),
            ),
            phantom: PhantomData,
        }
    }
}
