#!/bin/bash
if [ "$1" != "patch" ] && [ "$1" != "restore" ]; then
    echo "Usage: $0 <patch|restore> arg1 arg2" 
    exit 1
fi

if [ "$1" == "patch" ]; then
    
    if [ "$#" -ne 3 ]; then
        echo "Usage: $0 patch <symbol1,symbol2,...> <binary>"
        exit 1
    fi

    IFS=',' read -r -a SYMBOLS <<< "$2"

    NUM_SYMBOLS=${#SYMBOLS[@]}

    OBJCOPY_CMD="sudo objcopy"

    for SYMBOL in "${SYMBOLS[@]}"; do 
        OBJCOPY_CMD="${OBJCOPY_CMD} --redefine-sym ${SYMBOL}=__real_${SYMBOL}"
    done

    OBJCOPY_CMD="${OBJCOPY_CMD} $3 $3"

    eval "$OBJCOPY_CMD"
else
    if [ "$#" -ne 3 ]; then
        echo "Usage: $0 restore <copied_binary_path> <original_binary_path>>"
        exit 1
    fi

    CP_CMD="sudo cp $2 $3"

    eval "$CP_CMD"
fi
