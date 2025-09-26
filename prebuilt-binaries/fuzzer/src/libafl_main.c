extern int libafl_main(int argc, char **argv);


int __attribute__((weak)) main(int argc, char **argv) {
    return libafl_main(argc, argv);
}