#include <stdarg.h>
#include <stdio.h>
#include <stdint.h>

class ArgSize {
public:
  ArgSize(size_t size, bool is_string) : size(size), is_string(is_string) {}
  size_t size;
  bool is_string;
};
/// Ends with _ to avoid ruust form generating interfaces for this function
int __scanf_internal_symbolized_(int count, const char *format, va_list args);
void *get_w_addr();
FILE *open_devnull();
ArgSize compute_arg_size(void *dst_ptr, void *w_addr);


