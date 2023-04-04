#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <errno.h>

void handle_client(int c)
{
    char buf[8192];
    char *lastpos;
    int size;

    while (1) {
        size = recv(c, buf, 8192, 0);
        if (size == 0) {
            break;
        }
        lastpos = strchr(buf, '\n');
        send(c, buf, lastpos+1-buf, 0);
    }
}

int main()
{
    int s, c;
    int reuseaddr = 1;
    struct sockaddr_in6 addr;
    memset(&addr, sizeof(addr), 0);
    int pid;

    printf("%d %d %d\n", sizeof(addr), sizeof(struct sockaddr_in6), sizeof(addr.sin6_addr));

    s = socket(AF_INET6, SOCK_STREAM, 0);
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &reuseaddr, sizeof(reuseaddr));
    int ret = setsockopt(s, IPPROTO_IPV6, IPV6_V6ONLY, &reuseaddr, sizeof(reuseaddr));

    printf("%d errno: %d\n", ret, errno);


    addr.sin6_family = AF_INET6;


    addr.sin6_port = htons(8080);


    addr.sin6_addr = in6addr_any;

    for (int i = 0; i < sizeof(addr); ++i) 
        printf("%d: %x %d\n", i, ((unsigned char*)&addr)[i], ((unsigned char*)&addr)[i]);

    return;

    bind(s, (struct sockaddr *)&addr, sizeof(addr));
    listen(s, 5);

    while (1) {
        c = accept(s, NULL, NULL);
        pid = fork();
        if (pid == -1) {
            exit(1);
        } else if (pid == 0) {
            close(s);
            handle_client(c);
            close(c);
            return 0;
        } else {
            close(c);
            waitpid(pid, NULL, 0);
        }
    }
}