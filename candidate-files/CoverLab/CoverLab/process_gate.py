"""Windows child gate: wait until the parent has attached us to its Job Object."""
import msvcrt
import os
import subprocess
import sys


def main():
    fd = msvcrt.open_osfhandle(int(sys.argv[1]), os.O_RDONLY)
    try:
        token = os.read(fd, 1)
    finally:
        os.close(fd)
    if token != b'\x01':
        return 125
    return subprocess.call(sys.argv[2:])


if __name__ == '__main__':
    sys.exit(main())
