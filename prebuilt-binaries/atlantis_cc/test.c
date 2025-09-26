#include <unistd.h>
#include <stdlib.h>
#include <error.h>
#include <stdio.h>
#include <fcntl.h>

extern typeof(open) __real_open;

int __wrap_open(const char *pathname, int flags) {
	printf("Opening file: %s\n", pathname);
	return __real_open(pathname, flags);
}

int main(int argc, char **argv) {
	char contents[10];
	int fd;
	if ((fd = open("test.txt", 0)) < 0) {
		perror("open");
		exit(1);
	}
	return 0;
}
