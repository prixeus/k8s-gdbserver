define move_tcp_socket
    set var $OLDFD=$arg0
    set var $PORT=$arg1

    set var $LOWERPORT=$PORT/256
    set var $UPPERPORT=$PORT%256
    set var $NEWFD=(int)socket(2, 1, 0)
    set var $SOCKOPT=(void*)malloc(4)
    call *((int*)$SOCKOPT) = 1
    call (int)setsockopt($NEWFD, 1, 2, $SOCKOPT, 4)
    call (void)free($SOCKOPT)
    set var $ADDR=(void*)malloc(16)
    call (void*)memset($ADDR, 0, 16)
    call ((char*)$ADDR)[0] = 2
    call ((char*)$ADDR)[2] = $LOWERPORT
    call ((char*)$ADDR)[3] = $UPPERPORT
    call (int)bind($NEWFD, $ADDR, 16)
    call (int)listen($NEWFD, 10000000)
    call (void)free($ADDR)

    call (int)close($OLDFD)

    call (int)dup2($NEWFD, $OLDFD)
end

define move_tcp6_socket
    set var $OLDFD=$arg0
    set var $PORT=$arg1

    set var $LOWERPORT=$PORT/256
    set var $UPPERPORT=$PORT%256
    set var $NEWFD=(int)socket(10, 1, 0)
    set var $SOCKOPT=(void*)malloc(4)
    call *((int*)$SOCKOPT) = 1
    call (int)setsockopt($NEWFD, 1, 2, $SOCKOPT, 4)
    call (int)setsockopt($NEWFD, 41, 26, $SOCKOPT, 4)
    call (void)free($SOCKOPT)
    set var $ADDR=(void*)malloc(28)
    call (void*)memset($ADDR, 0, 28)
    call ((char*)$ADDR)[0] = 10
    call ((char*)$ADDR)[2] = $LOWERPORT
    call ((char*)$ADDR)[3] = $UPPERPORT
    call (int)bind($NEWFD, $ADDR, 28)
    print errno
    call (int)listen($NEWFD, 10000000)
    call (void)free($ADDR)

    call (int)close($OLDFD)

    call (int)dup2($NEWFD, $OLDFD)
end