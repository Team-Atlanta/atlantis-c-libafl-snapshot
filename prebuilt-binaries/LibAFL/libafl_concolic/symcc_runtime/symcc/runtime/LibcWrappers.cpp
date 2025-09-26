// This file is part of the SymCC runtime.
//
// The SymCC runtime is free software: you can redistribute it and/or modify it
// under the terms of the GNU Lesser General Public License as published by the
// Free Software Foundation, either version 3 of the License, or (at your
// option) any later version.
//
// The SymCC runtime is distributed in the hope that it will be useful, but
// WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
// FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General Public License
// for more details.
//
// You should have received a copy of the GNU Lesser General Public License
// along with SymCC. If not, see <https://www.gnu.org/licenses/>.

//
// Libc wrappers
//
// This file contains the wrappers around libc functions which add symbolic
// computations; using the wrappers frees instrumented code from having to link
// against an instrumented libc.
//
// We define a wrapper for function X with SYM(X), which just changes the name
// "X" to something predictable and hopefully unique. It is then up to the
// compiler pass to replace calls of X with calls of SYM(X).
//
// In general, the wrappers ask the solver to generate alternative parameter
// values, then call the wrapped function, create and store symbolic expressions
// matching the libc function's semantics, and finally return the wrapped
// function's result.

#include <algorithm>
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <sys/syscall.h>
#include <variant>
#include <vector>

#include <arpa/inet.h>
#include <fcntl.h>
#include <stdarg.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <syscall.h>
#include <unistd.h>

#include "Config.h"
#include "ScanfSymbolization.h"
#include "Shadow.h"
#include <Runtime.h>
#include "RuntimeCommon.h"

#define SYM(x) x##_symbolized
#define SYM_VARARG(x) x##_symbolized_vararg
namespace {

/// The file descriptor referring to the symbolic input.
int inputFileDescriptor = -1;

/// The current position in the (symbolic) input.
uint64_t inputOffset = 0;

/// Tell the solver to try an alternative value than the given one.
template <typename V, typename F>
void tryAlternative(V value, SymExpr valueExpr, F caller) {
  if (valueExpr) {
    _sym_push_path_constraint(
        _sym_build_equal(valueExpr,
                         _sym_build_integer(value, sizeof(value) * 8)),
        true, reinterpret_cast<uintptr_t>(caller));
  }
}

// A partial specialization for pointer types for convenience.
template <typename E, typename F>
void tryAlternative(E *value, SymExpr valueExpr, F caller) {
  tryAlternative(reinterpret_cast<intptr_t>(value), valueExpr, caller);
}

void maybeSetInputFile(const char *path, int fd) {
  auto *fileInput = std::get_if<FileInput>(&g_config.input);
  if (fileInput == nullptr)
    return;

  if (strstr(path, fileInput->fileName.c_str()) == nullptr)
    return;

  if (inputFileDescriptor != -1)
    std::cerr << "Warning: input file opened multiple times; this is not yet "
                 "supported"
              << std::endl;

  inputFileDescriptor = fd;
  inputOffset = 0;
}

} // namespace

void initLibcWrappers() {
  if (std::holds_alternative<StdinInput>(g_config.input)) {
    // Symbolic data comes from standard input.
    inputFileDescriptor = 0;
  }
}

bool dont_symbolize = false;
bool stdin_write_done = false;
bool stdin_dup2_done = false;
int stdin_pipe_memfd = 0;
std::unique_ptr<uint8_t[]> stdin_pipe_buffer;

void nginx_symbolizer_fini(void) __attribute__((destructor));

void nginx_symbolizer_fini(void) { dont_symbolize = true; }

extern "C" {

void *SYM(malloc)(size_t size) {
  auto *result = malloc(size);

  tryAlternative(size, _sym_get_parameter_expression(0), SYM(malloc));

  _sym_set_return_expression(nullptr);
  return result;
}

void *SYM(calloc)(size_t nmemb, size_t size) {
  auto *result = calloc(nmemb, size);

  tryAlternative(nmemb, _sym_get_parameter_expression(0), SYM(calloc));
  tryAlternative(size, _sym_get_parameter_expression(1), SYM(calloc));

  _sym_set_return_expression(nullptr);
  return result;
}

// See comment on lseek and lseek64 below; the same applies to the "off"
// parameter of mmap.

void *SYM(mmap64)(void *addr, size_t len, int prot, int flags, int fildes,
                  uint64_t off) {
  auto *result = mmap64(addr, len, prot, flags, fildes, off);
  _sym_set_return_expression(nullptr);

  if (result == MAP_FAILED) // mmap failed
    return result;

  if (fildes == inputFileDescriptor) {
    /* we update the inputOffset only when mmap() is reading from input file
     * HACK! update inputOffset with off parameter sometimes will be dangerous
     * We don't know whether there is read() before/after mmap,
     * if there is, we have to fix this tricky method :P
     */
    inputOffset = off + len;
    // Reading symbolic input.
    ReadWriteShadow shadow(result, len);
    uint8_t *resultBytes = (uint8_t *)result;
    std::generate(shadow.begin(), shadow.end(), [resultBytes, i = 0]() mutable {
      return _sym_get_input_byte(inputOffset, resultBytes[i++]);
    });
  } else if (!isConcrete(result, len)) {
    ReadWriteShadow shadow(result, len);
    std::fill(shadow.begin(), shadow.end(), nullptr);
  }

  tryAlternative(len, _sym_get_parameter_expression(1), SYM(mmap64));

  return result;
}

void *SYM(mmap)(void *addr, size_t len, int prot, int flags, int fildes,
                uint32_t off) {
  return SYM(mmap64)(addr, len, prot, flags, fildes, off);
}

int SYM(open)(const char *path, int oflag, mode_t mode) {
  auto result = open(path, oflag, mode);
  _sym_set_return_expression(nullptr);

  if (result >= 0)
    maybeSetInputFile(path, result);

  return result;
}

/*
ssize_t SYM(read)(int fildes, void *buf, size_t nbyte) {
  tryAlternative(buf, _sym_get_parameter_expression(1), SYM(read));
  tryAlternative(nbyte, _sym_get_parameter_expression(2), SYM(read));

  auto result = read(fildes, buf, nbyte);
  _sym_set_return_expression(nullptr);

  if (result < 0)
    return result;

  if (fildes == inputFileDescriptor) {
    // Reading symbolic input.
    _sym_make_symbolic(buf, result, inputOffset);
    inputOffset += result;
  } else if (!isConcrete(buf, result)) {
    ReadWriteShadow shadow(buf, result);
    std::fill(shadow.begin(), shadow.end(), nullptr);
  }

  return result;
}
*/

// lseek is a bit tricky because, depending on preprocessor macros, glibc
// defines it to be a function operating on 32-bit values or aliases it to
// lseek64. Therefore, we cannot know in general whether calling lseek in our
// code takes a 32 or a 64-bit offset and whether it returns a 32 or a 64-bit
// result. In fact, since we compile this library against LLVM which requires us
// to compile with "-D_FILE_OFFSET_BITS=64", we happen to know that, for us,
// lseek is an alias to lseek64, but this may change any time. More importantly,
// client code may call one or the other, depending on its preprocessor
// definitions.
//
// Therefore, we define symbolic versions of both lseek and lseek64, but
// internally we only use lseek64 because it's the only one on whose
// availability we can rely.

uint64_t SYM(lseek64)(int fd, uint64_t offset, int whence) {
  auto result = lseek64(fd, offset, whence);
  _sym_set_return_expression(nullptr);
  if (result == (off_t)-1)
    return result;

  if (whence == SEEK_SET)
    _sym_set_return_expression(_sym_get_parameter_expression(1));

  if (fd == inputFileDescriptor)
    inputOffset = result;

  return result;
}

uint32_t SYM(lseek)(int fd, uint32_t offset, int whence) {
  uint64_t result = SYM(lseek64)(fd, offset, whence);

  // Perform the same overflow check as glibc in the 32-bit version of lseek.

  auto result32 = (uint32_t)result;
  if (result == result32)
    return result32;

  errno = EOVERFLOW;
  return (uint32_t)-1;
}

FILE *SYM(fopen)(const char *pathname, const char *mode) {
  auto *result = fopen(pathname, mode);
  _sym_set_return_expression(nullptr);

  if (result != nullptr)
    maybeSetInputFile(pathname, fileno(result));

  return result;
}

FILE *SYM(fopen64)(const char *pathname, const char *mode) {
  auto *result = fopen64(pathname, mode);
  _sym_set_return_expression(nullptr);

  if (result != nullptr)
    maybeSetInputFile(pathname, fileno(result));

  return result;
}

void SYM(rewind)(FILE *stream) {
  rewind(stream);
  _sym_set_return_expression(nullptr);

  if (fileno(stream) == inputFileDescriptor) {
    inputOffset = 0;
  }
}

int SYM(fseek)(FILE *stream, long offset, int whence) {
  tryAlternative(offset, _sym_get_parameter_expression(1), SYM(fseek));

  auto result = fseek(stream, offset, whence);
  _sym_set_return_expression(nullptr);
  if (result == -1)
    return result;

  if (fileno(stream) == inputFileDescriptor) {
    auto pos = ftell(stream);
    if (pos == -1)
      return -1;
    inputOffset = pos;
  }

  return result;
}

int SYM(fseeko)(FILE *stream, off_t offset, int whence) {
  tryAlternative(offset, _sym_get_parameter_expression(1), SYM(fseeko));

  auto result = fseeko(stream, offset, whence);
  _sym_set_return_expression(nullptr);
  if (result == -1)
    return result;

  if (fileno(stream) == inputFileDescriptor) {
    auto pos = ftello(stream);
    if (pos == -1)
      return -1;
    inputOffset = pos;
  }

  return result;
}

int SYM(fseeko64)(FILE *stream, uint64_t offset, int whence) {
  tryAlternative(offset, _sym_get_parameter_expression(1), SYM(fseeko64));

  auto result = fseeko64(stream, offset, whence);
  _sym_set_return_expression(nullptr);
  if (result == -1)
    return result;

  if (fileno(stream) == inputFileDescriptor) {
    auto pos = ftello64(stream);
    if (pos == -1)
      return -1;
    inputOffset = pos;
  }

  return result;
}

int SYM(getc)(FILE *stream) {
  auto result = getc(stream);
  if (result == EOF) {
    _sym_set_return_expression(nullptr);
    return result;
  }

  if (fileno(stream) == inputFileDescriptor)
    _sym_set_return_expression(_sym_build_zext(
        _sym_get_input_byte(inputOffset++, result), sizeof(int) * 8 - 8));
  else
    _sym_set_return_expression(nullptr);

  return result;
}

int SYM(fgetc)(FILE *stream) {
  auto result = fgetc(stream);
  if (result == EOF) {
    _sym_set_return_expression(nullptr);
    return result;
  }

  if (fileno(stream) == inputFileDescriptor)
    _sym_set_return_expression(_sym_build_zext(
        _sym_get_input_byte(inputOffset++, result), sizeof(int) * 8 - 8));
  else
    _sym_set_return_expression(nullptr);

  return result;
}

int SYM(getchar)(void) { return SYM(getc)(stdin); }

int SYM(ungetc)(int c, FILE *stream) {
  auto result = ungetc(c, stream);
  _sym_set_return_expression(_sym_get_parameter_expression(0));

  if (fileno(stream) == inputFileDescriptor && result != EOF)
    inputOffset--;

  return result;
}

void *SYM(memcpy)(void *dest, const void *src, size_t n) {
  auto *result = memcpy(dest, src, n);

  tryAlternative(dest, _sym_get_parameter_expression(0), SYM(memcpy));
  tryAlternative(src, _sym_get_parameter_expression(1), SYM(memcpy));
  tryAlternative(n, _sym_get_parameter_expression(2), SYM(memcpy));

  _sym_memcpy(static_cast<uint8_t *>(dest), static_cast<const uint8_t *>(src),
              n);
  _sym_set_return_expression(_sym_get_parameter_expression(0));
  return result;
}

void *SYM(memset)(void *s, int c, size_t n) {
  auto *result = memset(s, c, n);

  tryAlternative(s, _sym_get_parameter_expression(0), SYM(memset));
  tryAlternative(n, _sym_get_parameter_expression(2), SYM(memset));

  _sym_memset(static_cast<uint8_t *>(s), _sym_get_parameter_expression(1), n);
  _sym_set_return_expression(_sym_get_parameter_expression(0));
  return result;
}

void SYM(bzero)(void *s, size_t n) {
  bzero(s, n);

  // No return value, hence no corresponding expression.
  _sym_set_return_expression(nullptr);

  tryAlternative(s, _sym_get_parameter_expression(0), SYM(bzero));
  tryAlternative(n, _sym_get_parameter_expression(1), SYM(bzero));

  // Concretize the memory region, which now is all zeros.
  ReadWriteShadow shadow(s, n);
  std::fill(shadow.begin(), shadow.end(), nullptr);
}

void *SYM(memmove)(void *dest, const void *src, size_t n) {
  tryAlternative(dest, _sym_get_parameter_expression(0), SYM(memmove));
  tryAlternative(src, _sym_get_parameter_expression(1), SYM(memmove));
  tryAlternative(n, _sym_get_parameter_expression(2), SYM(memmove));

  auto *result = memmove(dest, src, n);
  _sym_memmove(static_cast<uint8_t *>(dest), static_cast<const uint8_t *>(src),
               n);

  _sym_set_return_expression(_sym_get_parameter_expression(0));
  return result;
}

void SYM(bcopy)(const void *src, void *dest, size_t n) {
  tryAlternative(src, _sym_get_parameter_expression(0), SYM(bcopy));
  tryAlternative(dest, _sym_get_parameter_expression(1), SYM(bcopy));
  tryAlternative(n, _sym_get_parameter_expression(2), SYM(bcopy));

  bcopy(src, dest, n);

  // bcopy is mostly equivalent to memmove, so we can use our symbolic version
  // of memmove to copy any symbolic expressions over to the destination.
  _sym_memmove(static_cast<uint8_t *>(dest), static_cast<const uint8_t *>(src),
               n);

  // void function, so there is no return value and hence no expression for it.
  _sym_set_return_expression(nullptr);
}

char *SYM(strncpy)(char *dest, const char *src, size_t n) {
  tryAlternative(dest, _sym_get_parameter_expression(0), SYM(strncpy));
  tryAlternative(src, _sym_get_parameter_expression(1), SYM(strncpy));
  tryAlternative(n, _sym_get_parameter_expression(2), SYM(strncpy));

  auto *result = strncpy(dest, src, n);
  _sym_set_return_expression(nullptr);

  size_t srcLen = strnlen(src, n);
  size_t copied = std::min(n, srcLen);
  if (isConcrete(src, copied) && isConcrete(dest, n))
    return result;

  auto srcShadow = ReadOnlyShadow(src, copied);
  auto destShadow = ReadWriteShadow(dest, n);

  std::copy(srcShadow.begin(), srcShadow.end(), destShadow.begin());
  if (copied < n) {
    ReadWriteShadow destRestShadow(dest + copied, n - copied);
    std::fill(destRestShadow.begin(), destRestShadow.end(), nullptr);
  }

  return result;
}

const char *SYM(strchr)(const char *s, int c) {
  tryAlternative(s, _sym_get_parameter_expression(0), SYM(strchr));
  tryAlternative(c, _sym_get_parameter_expression(1), SYM(strchr));

  auto *result = strchr(s, c);
  _sym_set_return_expression(nullptr);

  auto *cExpr = _sym_get_parameter_expression(1);
  if (isConcrete(s, result != nullptr ? (result - s) : strlen(s)) &&
      cExpr == nullptr)
    return result;

  if (cExpr == nullptr)
    cExpr = _sym_build_integer(c, 8);
  else
    cExpr = _sym_build_trunc(cExpr, 8);

  size_t length = result != nullptr ? (result - s) : strlen(s);
  auto shadow = ReadOnlyShadow(s, length);
  auto shadowIt = shadow.begin();
  for (size_t i = 0; i < length; i++) {
    _sym_push_path_constraint(
        _sym_build_not_equal(
            (*shadowIt != nullptr) ? *shadowIt : _sym_build_integer(s[i], 8),
            cExpr),
        /*taken*/ 1, reinterpret_cast<uintptr_t>(SYM(strchr)));
    ++shadowIt;
  }

  return result;
}

int SYM(strcmp)(const char *a, const char *b) {
  auto aShadow = ReadOnlyShadow(a, (size_t)-1);
  auto bShadow = ReadOnlyShadow(b, (size_t)-1);
  auto aShadowIt = aShadow.begin();
  auto bShadowIt = bShadow.begin();
  auto null_byte = _sym_build_integer(0, 8);
  while (true) {
    _sym_push_path_constraint(
        _sym_build_not_equal(
            (*aShadowIt != nullptr) ? *aShadowIt : _sym_build_integer(*a, 8),
            null_byte),
        *a != '\0', reinterpret_cast<uintptr_t>(SYM(strcmp)) + 0);

    if (*a != '\0') {
      _sym_push_path_constraint(
          _sym_build_not_equal(
              (*bShadowIt != nullptr) ? *bShadowIt : _sym_build_integer(*b, 8),
              null_byte),
          *b != '\0', reinterpret_cast<uintptr_t>(SYM(strcmp)) + 1);

      if (*b != '\0') {
        _sym_push_path_constraint(
            _sym_build_equal(
                (*aShadowIt != nullptr) ? *aShadowIt
                                        : _sym_build_integer(*a, 8),
                (*bShadowIt != nullptr) ? *bShadowIt
                                        : _sym_build_integer(*b, 8)),
            *a == *b, reinterpret_cast<uintptr_t>(SYM(strcmp)) + 2);
        if (*a == *b) {
          a++;
          b++;
          ++aShadowIt;
          ++bShadowIt;
        } else {
          break;
        }
      } else {
        break;
      }
    } else {
      break;
    }
  }
  return *a - *b;
}

int SYM(strncmp)(const char *a, const char *b, size_t n) {
  auto aShadow = ReadOnlyShadow(a, n);
  auto bShadow = ReadOnlyShadow(b, n);
  auto aShadowIt = aShadow.begin();
  auto bShadowIt = bShadow.begin();
  auto null_byte = _sym_build_integer(0, 8);
  size_t i = 0;
  while (i < n) {
    _sym_push_path_constraint(
        _sym_build_not_equal(
            (*aShadowIt != nullptr) ? *aShadowIt : _sym_build_integer(*a, 8),
            null_byte),
        *a != '\0', reinterpret_cast<uintptr_t>(SYM(strncmp)) + 0);

    if (*a != '\0') {
      _sym_push_path_constraint(
          _sym_build_not_equal(
              (*bShadowIt != nullptr) ? *bShadowIt : _sym_build_integer(*b, 8),
              null_byte),
          *b != '\0', reinterpret_cast<uintptr_t>(SYM(strncmp)) + 1);

      if (*b != '\0') {
        _sym_push_path_constraint(
            _sym_build_equal(
                (*aShadowIt != nullptr) ? *aShadowIt
                                        : _sym_build_integer(*a, 8),
                (*bShadowIt != nullptr) ? *bShadowIt
                                        : _sym_build_integer(*b, 8)),
            *a == *b, reinterpret_cast<uintptr_t>(SYM(strncmp)) + 2);
        if (*a == *b) {
          a++;
          b++;
          ++aShadowIt;
          ++bShadowIt;
          i++;
        } else {
          break;
        }
      } else {
        break;
      }
    } else {
      break;
    }
  }
  return i == n ? 0 : *a - *b;
}

int SYM(memcmp)(const void *a, const void *b, size_t n) {
  tryAlternative(a, _sym_get_parameter_expression(0), SYM(memcmp));
  tryAlternative(b, _sym_get_parameter_expression(1), SYM(memcmp));
  tryAlternative(n, _sym_get_parameter_expression(2), SYM(memcmp));

  auto result = memcmp(a, b, n);
  _sym_set_return_expression(nullptr);

  if (isConcrete(a, n) && isConcrete(b, n))
    return result;

  auto aShadowIt = ReadOnlyShadow(a, n).begin_non_null();
  auto bShadowIt = ReadOnlyShadow(b, n).begin_non_null();
  auto *allEqual = _sym_build_equal(*aShadowIt, *bShadowIt);
  for (size_t i = 1; i < n; i++) {
    ++aShadowIt;
    ++bShadowIt;
    allEqual =
        _sym_build_bool_and(allEqual, _sym_build_equal(*aShadowIt, *bShadowIt));
  }

  _sym_push_path_constraint(allEqual, result == 0,
                            reinterpret_cast<uintptr_t>(SYM(memcmp)));
  return result;
}

int SYM(bcmp)(const void *a, const void *b, size_t n) {
  tryAlternative(a, _sym_get_parameter_expression(0), SYM(bcmp));
  tryAlternative(b, _sym_get_parameter_expression(1), SYM(bcmp));
  tryAlternative(n, _sym_get_parameter_expression(2), SYM(bcmp));

  auto result = bcmp(a, b, n);

  // bcmp returns zero if the input regions are equal and an unspecified
  // non-zero value otherwise. Instead of expressing this symbolically, we
  // directly ask the solver for an alternative solution (assuming that the
  // result is used for a conditional branch later), and return a concrete
  // value.
  _sym_set_return_expression(nullptr);

  // The result of the comparison depends on whether the input regions are equal
  // byte by byte. Construct the corresponding expression, but only if there is
  // at least one symbolic byte in either of the regions; otherwise, the result
  // is concrete.

  if (isConcrete(a, n) && isConcrete(b, n))
    return result;

  auto aShadowIt = ReadOnlyShadow(a, n).begin_non_null();
  auto bShadowIt = ReadOnlyShadow(b, n).begin_non_null();
  auto *allEqual = _sym_build_equal(*aShadowIt, *bShadowIt);
  for (size_t i = 1; i < n; i++) {
    ++aShadowIt;
    ++bShadowIt;
    allEqual =
        _sym_build_bool_and(allEqual, _sym_build_equal(*aShadowIt, *bShadowIt));
  }

  _sym_push_path_constraint(allEqual, result == 0,
                            reinterpret_cast<uintptr_t>(SYM(bcmp)));
  return result;
}

uint32_t SYM(ntohl)(uint32_t netlong) {
  auto netlongExpr = _sym_get_parameter_expression(0);
  auto result = ntohl(netlong);

  if (netlongExpr == nullptr) {
    _sym_set_return_expression(nullptr);
    return result;
  }

#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
  _sym_set_return_expression(_sym_build_bswap(netlongExpr));
#elif __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
  _sym_set_return_expression(netlongExpr);
#else
#error Unsupported __BYTE_ORDER__
#endif

  return result;
}

int SYM(pipe)(int pipefd[2]) {
  int ret = pipe(pipefd);
  if (ret == -1 || dont_symbolize) {
    return ret;
  }
  close(pipefd[0]);
  close(pipefd[1]);

  stdin_pipe_memfd = syscall(SYS_memfd_create, "_sym_input_data", 0);
  if (stdin_pipe_memfd == -1) {
    return -1;
  }
  pipefd[0] = stdin_pipe_memfd;
  pipefd[1] = stdin_pipe_memfd;
  return ret;
}

int SYM(write)(int fd, const void *buf, size_t count) {
  int ret = write(fd, buf, count);
  if (ret == -1 || dont_symbolize) {
    return ret;
  }
  if (fd != stdin_pipe_memfd) {
    return ret;
  }

  // from now on, we only handle the case where fd is stdin_pipe_memfd
  if (stdin_write_done) {
    // raise error
    std::cerr << "write(pipefd, ...) cannot be done more than twice. Make sure "
                 "your harness conforms to the setup_pipe_data format"
              << std::endl;
    return -1;
  }
  stdin_pipe_buffer = std::make_unique<uint8_t[]>(count);
  memcpy(stdin_pipe_buffer.get(), buf, ret);
  _sym_memcpy(stdin_pipe_buffer.get(), static_cast<const uint8_t *>(buf), ret);

  // reset the offset to zero
  if (lseek(fd, 0, SEEK_SET) == -1) {
    std::cerr << "Failed to lseek the memfd to 0" << std::endl;
    return -1;
  }

  stdin_write_done = true;
  return ret;
}

int SYM(read)(int fd, void *buf, size_t count) {
  off_t offset;
  int ret;
  if ((fd == STDIN_FILENO && stdin_write_done) &&
      (offset = lseek(fd, 0, SEEK_CUR)) == -1) {
    std::cerr << "Failed to measure the offset of memfd" << std::endl;
    return -1;
  }
  ret = read(fd, buf, count);
  if (dont_symbolize) {
    return ret;
  }
  if (!(fd == STDIN_FILENO && stdin_write_done)) {
    return ret;
  }
  /* handle case for setup_pipe_data in LLVMFuzzerTestOneInput */
  memcpy(buf, stdin_pipe_buffer.get() + offset, count);
  _sym_memcpy(static_cast<uint8_t *>(buf), stdin_pipe_buffer.get() + offset,
              count);
  return ret;
}

int SYM(dup2)(int oldfd, int newfd) {
  int ret = dup2(oldfd, newfd);
  if (newfd == STDIN_FILENO) {
    if (oldfd != stdin_pipe_memfd) {
      // An unexpected fd is dup2'ed to stdin
      std::cerr << "An unexpected fd(" << oldfd << ") is dup2'ed to stdin"
                << std::endl;
      return -1;
    } else {
      stdin_dup2_done = true;
      return ret;
    }
  }
  return ret;
}

/*
 * count: # of varidadic arguments provided. this may be different from the
 * return value of scanf nbytes: # of bytes consumed by scanf, this is
 * measured by utilizing the %n modifier format: format string of the scanf
 * call without the appended %n
 */
int SYM_VARARG(__isoc99_scanf)(int count, const char *format, ...) {

  va_list args;
  va_start(args, format);
  int ret = __scanf_internal_symbolized_(count, format, args);
  va_end(args);
  return ret;
}

size_t SYM(fread)(void *ptr, size_t size, size_t nmemb, FILE *stream) {
  tryAlternative(ptr, _sym_get_parameter_expression(0), SYM(fread));
  tryAlternative(size, _sym_get_parameter_expression(1), SYM(fread));
  tryAlternative(nmemb, _sym_get_parameter_expression(2), SYM(fread));

  auto result = fread(ptr, size, nmemb, stream);
  _sym_set_return_expression(nullptr);

  if (fileno(stream) == inputFileDescriptor) {
    // Reading symbolic input.
    _sym_make_symbolic(ptr, result * size, inputOffset);
    inputOffset += result * size;
  } else if (!isConcrete(ptr, result * size)) {
    ReadWriteShadow shadow(ptr, result * size);
    std::fill(shadow.begin(), shadow.end(), nullptr);
  }

  return result;
}

char *SYM(fgets)(char *str, int n, FILE *stream) {
  tryAlternative(str, _sym_get_parameter_expression(0), SYM(fgets));
  tryAlternative(n, _sym_get_parameter_expression(1), SYM(fgets));

  auto result = fgets(str, n, stream);
  _sym_set_return_expression(_sym_get_parameter_expression(0));

  if (fileno(stream) == inputFileDescriptor) {
    // Reading symbolic input.
    const auto length = sizeof(char) * strlen(str);
    _sym_make_symbolic(str, length, inputOffset);
    inputOffset += length;
  } else if (!isConcrete(str, sizeof(char) * strlen(str))) {
    ReadWriteShadow shadow(str, sizeof(char) * strlen(str));
    std::fill(shadow.begin(), shadow.end(), nullptr);
  }

  return result;
}
} // extern "C"

// TODO: think about floats and doubles
int __scanf_internal_symbolized_(int count, const char *format, va_list args) {

  static int scanf_nonce = 0;

  void *w_addr;
  char *buf, *tmp_buf, *r_addr_buf;
  FILE *devnull_fp;
  int nbytes_write, input_begin = 0, input_end = 0, this_scanf_nonce, ret;
  std::vector<void *> r_addrs;
  std::vector<ArgSize> argument_sizes;

  input_begin = static_cast<int>(ftell(stdin));
  va_list args_copy;
  va_copy(args_copy, args);
  ret = vscanf(format, args);
  va_end(args_copy);
  input_end = static_cast<int>(ftell(stdin));

  if (dont_symbolize || !stdin_write_done) {
    return ret;
  }

  this_scanf_nonce = scanf_nonce++;

  tmp_buf = (char *)malloc(input_end - input_begin);
  if (tmp_buf == NULL) {
    return ret;
  }

  // symbolize
  w_addr = get_w_addr();
  if (w_addr == NULL) {
    goto done1;
  }
  r_addr_buf = (char *)malloc(16 * count);
  if (r_addr_buf == NULL) {
    goto done2;
  }
  for (int i = 0; i < count; i++) {
    uint8_t *r_addr = (uint8_t *)(r_addr_buf + 16 * i);
    uint64_t w_addr_int = (uint64_t)w_addr;
    for (int j = 0; j < 8; j++) {
      // Avoid any conincidence with the w_addr
      r_addr[j] = (w_addr_int & 0xff) + 1;
      w_addr_int >>= 8;
    }
    memset(r_addr + 8, 0xcc, 8);
    r_addrs.push_back(r_addr);
  }

#define DO_SPRINTF(...)                                                        \
  devnull_fp = open_devnull();                                                 \
  if (!devnull_fp) {                                                           \
    goto done3;                                                                \
  }                                                                            \
  nbytes_write = fprintf(devnull_fp, format, __VA_ARGS__);                     \
  fclose(devnull_fp);                                                          \
  buf = (char *)malloc(nbytes_write + 10);                                     \
  if (buf == NULL) {                                                           \
    goto done3;                                                                \
  }                                                                            \
  snprintf(buf, nbytes_write + 10, format, __VA_ARGS__);

#define DO_SSCANF(...)                                                         \
  int ret = sscanf(buf, format, __VA_ARGS__);                                  \
  free(buf);                                                                   \
  if (ret != count) {                                                          \
    goto done3;                                                                \
  }

  switch (count) {
  case 1: {
    DO_SPRINTF(w_addr);
    DO_SSCANF(r_addrs[0]);
    break;
  }
  case 2: {
    DO_SPRINTF(w_addr, w_addr);
    DO_SSCANF(r_addrs[0], r_addrs[1]);
    break;
  }
  case 3: {
    DO_SPRINTF(w_addr, w_addr, w_addr);
    DO_SSCANF(r_addrs[0], r_addrs[1], r_addrs[2]);
    break;
  }
  case 4: {
    DO_SPRINTF(w_addr, w_addr, w_addr, w_addr);
    DO_SSCANF(r_addrs[0], r_addrs[1], r_addrs[2], r_addrs[3]);
    break;
  }
  case 5: {
    DO_SPRINTF(w_addr, w_addr, w_addr, w_addr, w_addr);
    DO_SSCANF(r_addrs[0], r_addrs[1], r_addrs[2], r_addrs[3], r_addrs[4]);
    break;
  }
  default: {
    goto done3;
  }
  }

  /* Based on the observations made above we can now determine the size (in
   bytes) of each argument Use _sym_mem_write to symbolize the memory region of
   each argument
   */

  for (int i = 0; i < count; i++) {
    auto arg_size = compute_arg_size(r_addrs[i], w_addr);

    if (arg_size.is_string) {
      /* Arg matched to %s. We don't symbolize in this case */
      continue;
    } else {
      // TODO: deal with non little-endian ISAs
      bool success = ret >= i + 1;
      SymExpr value = _sym_build_scanf_extract(
          // last argument denotes success of scanf to read that argument
          format, input_begin, input_end, i, arg_size.size, this_scanf_nonce,
          success);
      uint8_t *addr = va_arg(args, uint8_t *);
      _sym_write_memory(addr, arg_size.size, value, true);

      // This path constraint emulates the return value behavior of scanf
      // Because it's extremely hard to symbolically represent the return value
      // of scanf, we instead 'emulate' this behavior by placing imposing a
      // constraint x == 0 for each of the arguments. This helps us deail with
      // situations such as if (scanf("%d %d", &a, &b) != 2) { ... } where the
      // return value of scanf is used to check if the correct number of
      // arguments were read and if not, the program does not operate on the
      // arguments read at all which would cause no constraints to be placed on
      // the ScanfExtract expressions
      if (!success) {
        // TODO: change the site id to something more meaningful?
        SymExpr zero_const = _sym_build_integer(0, arg_size.size * 8);
        _sym_push_path_constraint(_sym_build_equal(value, zero_const), false,
                                  0x133713370000 + this_scanf_nonce + i);
      }
    }
  }
done3:
  free(r_addr_buf);
done2:
  free(w_addr);
done1:
  free(tmp_buf);
  return ret;
}

ArgSize compute_arg_size(void *dst_ptr_, void *w_addr_) {
  uint8_t *dst_ptr = (uint8_t *)dst_ptr_;
  uint8_t *w_addr = (uint8_t *)w_addr_;
  uint64_t w_addr_int = (uint64_t)w_addr;
  int i = 0;
  ArgSize ret(0, false);
  if (dst_ptr[0] == w_addr[0]) {
    ret.is_string = true;
    return ret;
  }
  for (i = 0; i < 8; i++) {
    if (dst_ptr[i] == (w_addr_int & 0xff) + 1) {
      break;
    }
    w_addr_int >>= 8;
  }
  ret.size = i;
  ret.is_string = false;
  return ret;
}

void *get_w_addr() {
  /* The string "ABCDEFGH" is 0x4847464544434241 in little-endian
   * Thus, it can never coincide with a heap address (non-canonical &&
   * unaligned). The relation bytes[0] = 'A' is sufficient proof that the
   * argument type is string and not (int, float, char, ...)
   */
  void *addr = malloc(16);
  strcpy((char *)addr, "ABCDEFGH");
  return addr;
}

FILE *open_devnull() {
  FILE *fp = fopen("/dev/null", "w");
  return fp;
}
