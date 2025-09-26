import re
import os
import json
from collections import deque
import tree_sitter_cpp
import logging
from pathlib import Path
from tree_sitter import Language, Parser, Tree, Node
from typing import Any, Optional, Dict, Callable
from collections import defaultdict
import random
from libAgents.agents import AgentBase, DeepThinkAgent
from libAgents.utils import Project, extract_script_from_response
from libAgents.config import get_model
import time
from libAgents.model import generate_text

CPP_LANGUAGE = Language(tree_sitter_cpp.language())
cpp_parser = Parser(CPP_LANGUAGE)
REACHABILITY_SHARE_DIR = os.environ.get("REACHABILITY_SHARE_DIR")
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

suffix_blacklist = ['.yaml', '.json', '.bazel', '.txt']
fuzz_entry = ['LLVMFuzzerInitialize', 'LLVMFuzzerTestOneInput']

def get_system_prompt(cp_name):
    prompt = f"""
    INSTRUCTIONS:
    You are an expert in code analysis and fuzz testing for the project {cp_name}. You will be given a new commit
    in diff mode, this diff will introduce at least one bug. This diff may:
    - Introduce new bugs in the added code
    - Add function calls to existing buggy functions
    - Modify control flow in ways that expose existing vulnerabilities
    OBJECTIVE:
    - You are expected to identify potential bugs and generate the input to trigger the bug at the fuzzing entry.
    - You will be given detailed subtasks to achieve this goal. Mainly summarizing function behaviors 
    from the end of the call chain, and you are expected to summarize functions based on the code base 
    and summary of the callee, the summary contains the constraints that can trigger bugs.
    NOTES
    - Always reason based on both the diff and the context of existing code.
    - Focus on helping find real, triggerable bugs, not just theoretical ones.

    Keep this in mind for all the following tasks."""
    return prompt

def get_start_prompt(diff_code):
    prompt = f"""Now looking at this diff introduced in the function, {diff_code},
    you need to determine if there's any bug introduced by this commit,
    do you think you need to start with analyzing with some callees, 
    or you can directly start with this function, 
    if you can start with this function, return an empty python style list: [], otherwise 
    return a list with functions you want to start with, following the format of ['func1','func2'], 
    only output a list without any explanation."""
    return prompt

def get_first_call_prompt(func_name, func_def):
    prompt = f""" This is the definition of `{func_name}`: 
    `{func_def}` \n Is there any potential bugs
    in this function? If the commit modifies this function, pay more
    attention to the bugs introduced by diff. If there are any bugs
    introduced by the diff, generate input constraints that could trigger bugs in this function.
    Return JSON format with:
    - bug_identified: <yes/no>, yes if the corresponding bug is present, otherwise no
    - parameter_constraints: Conditions on each parameter
    - global_state_constraints: Any required global state
    - bug_trigger_conditions: Specific conditions that trigger bugs
    Do not include any extra explanation, only output a json.
    ```json
    {{
        "Out-of-bound memory access": [
            {{  "bug_identified": <yes/no>, 
                "parameter_constraints": "<summarized constraints>",
                "global_state_constraints": "<summarized constraint>",
                "bug_trigger_conditions" : "<buggy conditions>"
            }}
        ],
        "Double-free": [
            {{  "bug_identified": <yes/no>,
                "parameter_constraints": "<summarized constraints>",
                "global_state_constraints": "<summarized constraint>",
                "bug_trigger_conditions" : "<buggy conditions>"
            }}
        ],
        "Use-after-free": [
            {{
                "bug_identified": <yes/no>,
                "parameter_constraints": "<summarized constraints>",
                "global_state_constraints": "<summarized constraint>",
                "bug_trigger_conditions" : "<buggy conditions>"
            }}
        ],
        "Use-before-initialization": [
            {{
                "bug_identified": <yes/no>,
                "parameter_constraints": "<summarized constraints>",
                "global_state_constraints": "<summarized constraint>",
                "bug_trigger_conditions" : "<buggy conditions>"
            }}
        ],
        "Null-or-invalid-pointer-dereference": [
            {{
                "bug_identified": <yes/no>,
                "parameter_constraints": "<summarized constraints>",
                "global_state_constraints": "<summarized constraint>",
                "bug_trigger_conditions" : "<buggy conditions>"
            }}
        ]
    }}
    ```
    """
    return prompt

def get_function_call_prompt(last_func, func_name, func_def):
    prompt = f"""Based on the summary of the `{last_func}`, summarize the pre-condition
    and post-condition of `{func_name}` to make the caller have a bug-free call to it, 
    using the same json format, the definition is `{func_def}`, 
    Each condition should describe relationships between the function's arguments, 
    return values, and any constants involved. If there are any bugs found in the callee
    and put in the summary, then you need to analyze the buggy input of the callee
    and update the conditions to trigget the bug according to the new calling context.  
    The JSON format will be:
    {{
        "Out-of-bound memory access": [
            {{
                "pre-condition": "<summarized condition>",
                "post-condition": "<summarized condition>",
                "buggy input for function" : <buggy input of each argument>
            }}
        ],
        "Double-free": [
            {{
                "pre-condition": "<summarized condition>",
                "post-condition": "<summarized condition>",
                "buggy input for function" : <buggy input of each argument>
            }}
        ],
        "Use-after-free": [
            {{
                "pre-condition": "<summarized condition>",
                "post-condition": "<summarized condition>",
                "buggy input for function" : <buggy input of each argument>
            }}
        ],
        "Use-before-initialization": [
            {{
                "pre-condition": "<summarized condition>",
                "post-condition": "<summarized condition>",
                "buggy input for function" : <buggy input of each argument>
            }}
        ],
        "Null-or-invalid-pointer-dereference": [
            {{
                "bug_identified": <yes/no>,
                "parameter_constraints": "<summarized constraints>",
                "global_state_constraints": "<summarized constraint>",
                "bug_trigger_conditions" : "<buggy conditions>"
            }}
        ]
    }}
    Only output the JSON object. Do not include explanations or extra text.
    """
    return prompt
def get_seeds_generation_prompt(entry_func, harness_code, last_func, call_path):
    prompt = f"""
    Generate fuzzing input based on bugs found in {last_func}, this input will be passed to the 
    fuzz entry function {entry_func}, and throguh the call chain : {"\n".join(call_path)}, the target is to 
    trigger the bug that introduced by the diff. The definition of the entry function is :
    {harness_code}

    ## SCRIPT USAGE GUIDE:
        - Write elegant, precise, and error-free Python code.
        - The script must implement a function named `gen_one_seed` that returns a seed for the fuzzing harness in bytes.

    ## CRITICAL REQUIREMENTS:
        - We prefer self-contained scripts, but you can use third-party packages when you have to.
        - If you use third-party packages, make sure you correctly use the APIs.
        - Remember to do auto-testing by running the script to mitigate the import errors and syntax errors.

    ## OUTPUT FORMAT:
        - Show me the generated script enclosed within `<script>` and `</script>` tags:
        - We'll extract the runnable script from <script>...</script> tags.
        <script>
        # other codes ...
        def gen_one_seed() -> bytes: # for real usage
            # Your diff-aware and analysis-informed seed generation logic here
            pass

        if __name__ == "__main__": # for auto-testing
            for _ in range(30): # for robust testing due to inner randomness
                gen_one_seed()
        </script>
    """
    return prompt
def findBugs(ans):
    if '```' in ans:
        match = re.search(r"```json(.*?)```", ans, re.DOTALL)
        if match:
            json_str = match.group(1).strip()
            print(f"matched : {json_str}")
            json_obj = json.loads(json_str.strip())
            for category, entries in json_obj.items():
                for entry in entries:
                    if entry.get("bug_identified") == "yes":
                        return True
    return False
class DiffAnalyzer(AgentBase):
    def __init__(
        self, 
        model: str,
        project_bundle: Project,
        harness_id: str,
        timeout: int = 1500,
        cache_type: Optional[str] = None,
        cache_expire_time: int = 1800,
    ):
        super().__init__(project_bundle)
        self.project_bundle = project_bundle
        self.harness_id = harness_id

        #cp_name, source_dir, diff_path, cg_path, harness_file, model_name):
        self.source_dir = self.project_bundle.repo_path #source_dir
        self.cp_name = self.project_bundle.name #cp_name
        self.harness_file = self.project_bundle.harness_path_by_name(
                self.harness_id
            )
        #self.diff_path=project_bundle.ref_diff #diff_path
        self.ref_diff = project_bundle.ref_diff
        self.model = model
        self.diff_line_numbers = {}
        self.diff_funcs = {}
        self.diffs = []
        self.funcs = []
        self.func_to_diff = {}
        self.callgraph_path = ""
        self.call_chain = {}
        self.func_hunk = {}
        self.func_sigs = {}
    def get_diff_lines(self):
        diff_text = self.ref_diff
        diff_lines = {}
        diff_funcs = {}
        func_to_diff = {}
        file_pattern = re.compile(r'^diff --git a/(.*?) b/(.*?)$')
        hunk_pattern = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
        filename = ""
        lines = diff_text.split("\n")
        i = 0
        length = len(lines)
        hunk_content = ""
        func_name = ""
        while i<length:
            line = lines[i]
            file_match = file_pattern.match(line)
            if file_match:
                filename = file_match.group(1)
                if filename not in diff_lines and (not filename.endswith(tuple(suffix_blacklist))):
                    diff_lines[filename] = []
                i+=1
                continue
            hunk_match = hunk_pattern.match(line)
            if hunk_match:
                old_start, old_lines, new_start, new_lines = hunk_match.groups()
                
                if not filename.endswith(tuple(suffix_blacklist)):
                    start = int(new_start)+3
                    end = max(int(new_start) + int(new_lines or 1) - 4, 1)
                    diff_lines[filename].append((start, end))
                    func_name = self.get_func_from_callgraph(filename, start, end)
                    if not func_name:
                        i+=1
                        continue
                    hunk_content = lines[i]+"\n"
                    i+=1
                    while(i<length):
                        hunk_match = hunk_pattern.match(lines[i])
                        if hunk_match:
                            if func_name not in self.func_hunk:
                                self.func_hunk[func_name] = []
                            self.func_hunk[func_name].append(hunk_content)
                            break
                        hunk_content = hunk_content+lines[i]+"\n"
                        i+=1
                continue
            i+=1
        if func_name not in self.func_hunk:
            self.func_hunk[func_name] = []
        if func_name != None:
            self.func_hunk[func_name].append(hunk_content)
        self.diff_line_numbers = diff_lines
        #logger.info(f"diff_line_numbers : {self.diff_line_numbers}")
        self.get_function_name_from_callgraph()
        #logger.info(f"diff_funcs : {self.diff_funcs}")
    def get_func_from_callgraph(self, filename, start, end):
        try:
            for result_file in os.listdir(REACHABILITY_SHARE_DIR):
                if result_file.startswith('whole-'):
                    file_path = os.path.join(REACHABILITY_SHARE_DIR, result_file)
                    self.callgraph_path = file_path
                    break
            with open(self.callgraph_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)

            for node in json_data.get("graph", {}).get("nodes", []):
                func_info = node.get("data", {})
                if func_info:
                    func = self.get_func_name(func_info, filename, start, end)
                    if func:
                        return func
        except Exception as e:
            logger.info(f"Error : {e}")
    def get_func_name(self, data, filename, start, end):
        start = int(start)
        end = int(end)
        
        try:
            func_name = data.get("func_name")
            start_line = data.get("start_line")
            end_line = data.get("end_line")
            file_name = data.get("file_name")
            if (start_line is not None) and (end_line is not None):
                start_line = int(start_line)
                end_line = int(end_line)
                if file_name.endswith(filename):
                    if not ((start_line > end) or (end_line < start)):
                        return func_name
        except Exception as e:
            logger.info(f"Error : {e}")
    def get_function_name_from_callgraph(self):
        try:
            for result_file in os.listdir(REACHABILITY_SHARE_DIR):
                if result_file.startswith('whole-'):
                    #logger.info(f"result_file : {result_file}")
                    file_path = os.path.join(REACHABILITY_SHARE_DIR, result_file)
                    self.callgraph_path = file_path
                    break
            with open(self.callgraph_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)

            for node in json_data.get("graph", {}).get("nodes", []):
                func_info = node.get("data", {})
                if func_info:
                    func = self.get_overlap_func(func_info)
        except Exception as e:
            logger.info(f"Error : {e}")
    def get_overlap_func(self, data):
        diff_lines = self.diff_line_numbers
        try:
            func_name = data.get("func_name")
            start_line = data.get("start_line")
            end_line = data.get("end_line")
            file_name = data.get("file_name")
            if (start_line is not None) and (end_line is not None):
                start_line = int(start_line)
                end_line = int(end_line)
            
                for diff_file_name in diff_lines:
                    if file_name.endswith(diff_file_name):
                        for ranges in diff_lines[diff_file_name]:
                            start, end = ranges
                            start = int(start)
                            #logger.info(f"start = {str(start)}, end = {str(end)}, start_line = {str(start_line)}, end_line = {str(end_line)}")
                            end = int(end)
                            if not ((start_line > end) or (end_line < start)):
                                if diff_file_name not in self.diff_funcs:
                                    self.diff_funcs[diff_file_name] = []
                                if func_name not in self.func_to_diff:
                                    self.func_to_diff[func_name] = []
                                #logger.info(f"diff_funcs[{diff_file_name}].append({func_name})")
                                self.diff_funcs[diff_file_name].append(func_name)
                                self.func_to_diff[func_name].append((start, end))
        except Exception as e:
            logger.info(f"Error : {e}")

    def load_call_graph(self):
        with open(self.callgraph_path, "r") as f:
            json_data = json.load(f)
    
        call_graph = defaultdict(list)
        id_to_func = {}
        func_to_info = {}

        # map from id to func_name
        for node in json_data["graph"]["nodes"]:
            node_id = node["id"]
            func_name = node["data"]["func_name"]
            id_to_func[node_id] = func_name
            self.func_sigs[func_name] = node["data"]["func_sig"]

        # adjacency matrix {source_id: [target_id1, target_id2, ...]}
        for link in json_data["graph"]["links"]:
            source = link["source"]
            target = link["target"]
            call_graph[source].append(target)
    
        entrypoint = json_data["entrypoint"][0]
        entrypoint_id = entrypoint["id"]
        entrypoint_func = entrypoint["data"]["func_name"]
    
        return call_graph, id_to_func, entrypoint_id, entrypoint_func

    def find_all_paths(self, call_graph, id_to_func, start_id, target_func, max_depth=10):
        logger.info(f"find paths for {target_func}")
        try:
            all_paths = []
            stack = [(start_id, [id_to_func[start_id]], set([start_id]))]  # (current_id, path, visited)
    
            while stack:
                current_id, path, visited = stack.pop()
                current_func = id_to_func[current_id]
        
                if current_func == target_func:
                    all_paths.append(path.copy())
                    continue

                if len(path) >= max_depth:
                    continue
        
                for neighbor_id in call_graph.get(current_id, []):
                    if neighbor_id not in visited and neighbor_id in id_to_func:
                        new_visited = visited.copy()
                        new_visited.add(neighbor_id)
                        new_path = path.copy()
                        #logger.info(f"add func {id_to_func[neighbor_id]}")
                        new_path.append(id_to_func[neighbor_id])
                        stack.append((neighbor_id, new_path, new_visited))
            return all_paths
        except Exception as e:
            logger.info(f"Error : {e}")

    def get_call_chain_for_diff_funcs(self):
        try:
            call_graph, id_to_func, entrypoint_id, entrypoint_func = self.load_call_graph()
            for func in self.func_to_diff:
                paths = self.find_all_paths(call_graph, id_to_func, entrypoint_id, func)
                self.call_chain[func] = paths
        except Exception as e:
            logger.info(f"Error : {e}")
    async def quote_each_diff(self):
        try:
            #sys instructions for tasks
            sys_prompt = get_system_prompt(self.cp_name)
            logger.info(f"sys_prompt : {sys_prompt}")
            res = await self.prompt_llm(sys_prompt, "user")

            for func in self.func_hunk:
                if random.choice([True, False]):
                    continue
                start_prompt = get_start_prompt(self.func_hunk[func])
                str_list = await self.prompt_llm(start_prompt, "user")
                if '```' in str_list:
                    match = re.search(r"```python(.*?)```", str_list, re.DOTALL)
                    if match:
                        str_list = match.group(1).strip()

                if str_list != "[]":
                    func_list = json.loads(str_list.strip())
                    new_path = []
                    for path in self.call_chain[func]:
                        for f in func_list:
                            tmp = path
                            tmp.append(f)
                            new_path.append(tmp)
                    self.call_chain[func]=new_path

                for path in self.call_chain[func]:
                    if random.choice([True, False]):
                        continue
                    fuzz_entry = path[0]
                    end_func = path[-1]
                    func_def = self.get_func_def(end_func)
                    prompt = get_first_call_prompt(end_func, func_def)
                    ans = await self.prompt_llm(prompt, "user")

                    if not findBugs(ans):
                        r_path = path
                        r_path = r_path[:-1]
                        r_path.reverse() 
                        #logger.info(f"reverse path = {r_path}")
                        last_func = end_func
                        for f in r_path:
                            if f in fuzz_entry:
                                break

                            func_def = self.get_func_def(f)
                            prompt = get_function_call_prompt(last_func, f, func_def)
                            ans = await self.prompt_llm(prompt, "user")
                        
                            last_func = f
                            if(findBugs(ans)):
                                break
                    harness_code = self.get_func_def(fuzz_entry)
                    if harness_code == None:
                        harness_code = self.get_harness_code(fuzz_entry)
                    func_sigs = self.get_func_sigs_for_call_chain(path)
                    prompt = get_seeds_generation_prompt(fuzz_entry, harness_code, last_func, func_sigs)
                    ans = await self.prompt_llm(prompt, "user")
                    model = get_model("agent", override_model=self.model)
                    script = await extract_script_from_response(ans, self.model)
                    return script
        except Exception as e:
            logger.info(f"Error : {e}")
    def get_func_sigs_for_call_chain(self, path):
        sigs = []
        for func in path:
            func_sig = self.func_sigs[func]
            sigs.append(func_sig)
        return sigs
    def get_harness_code(self, func_name):
        file_name = self.harness_file
        source_code = ""
        if os.path.exists(file_name):
            with open(file_name, 'r') as file:
                source_code = file.read()
        else:
            logger.info(f"{file_name} not found.")
            return False
        logger.info(f"Traverse the source code : {file_name}")
        source_lines = source_code.split("\n")
        tree = cpp_parser.parse(bytes(source_code, "utf8"))
        def traverse(node):
            if node.type == "function_definition":
                for child in node.children:
                    if child.type == "function_declarator":
                        for sub_child in child.children:
                            if sub_child.type == "identifier":
                                cur_func = sub_child.text.decode()
                                if cur_func == func_name:
                                    start_line = node.start_point[0] + 1
                                    end_line = node.end_point[0] + 1
                                    func_definition = ''.join(source_lines[start_line-1:end_line])
                                    return func_definition
            for child in node.children:
                result = traverse(child)
                if result is not None:
                    return result
        traverse(tree.root_node)
        return None

    def get_func_def(self, func_name):
        with open(self.callgraph_path, "r") as f:
            json_data = json.load(f)
        file_name = ""
        start_line = 0
        end_line = 0
        for node in json_data["graph"]["nodes"]:
            node_id = node["id"]
            name = node["data"]["func_name"]
            if func_name == name:
                file_name = node["data"]["file_name"]
                logger.info(f"file_name = {file_name}")
                start_line = node["data"]["start_line"]
                end_line = node["data"]["end_line"]
                break
        for filepath in Path(self.source_dir).rglob("*"):
            if filepath.is_file():
                source_file = str(filepath).split("/")[-1]
                if file_name.endswith(source_file):
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                
                        start_idx = max(0, start_line - 1)
                        end_idx = min(len(lines), end_line)
                
                        return "".join(lines[start_idx:end_idx])
            
                    except Exception as e:
                        logger.info(f"Error reading {filepath}: {e}")
                        return None
        return None

    async def prompt_llm(self, msg, role):
        model = get_model("agent", override_model=self.model)
        response = await generate_text(
            model,
            msg,
        )
        return response.object
        
    async def run(self):
        self.get_diff_lines()
        self.get_call_chain_for_diff_funcs()
        return await self.quote_each_diff()
    