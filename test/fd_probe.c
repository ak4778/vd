#include <stdio.h>
#include <winsock2.h>
int main(void) {
    fd_set s;
    FD_ZERO(&s);
    printf("FD_SETSIZE=%d sizeof(fd_set)=%d capacity=%d\n",
        (int)FD_SETSIZE, (int)sizeof(fd_set), (int)((sizeof(fd_set)-sizeof(u_int))/sizeof(SOCKET)));
    int n = 0;
    for (SOCKET i = 100; i < 200; i++) { FD_SET(i, &s); n++; }
    printf("added %d -> fd_count=%d\n", n, (int)s.fd_count);
    return 0;
}