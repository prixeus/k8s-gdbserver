#!/bin/bash

CONFIGFILE="k8s-dbgserver.json"
NAMESPACE=""
POD_NAME=""
CONT_NAME=""
PID=""
GOLANG=""
PIDFILE="k8s-dbgserver.pid"
PORTFILE="k8s-dbgserver.port"
LOGFILE="k8s-dbgserver.log"
START_TIMEOUT=120

function_get_config () {
    NAMESPACE=$(cat "${CONFIGFILE}" | jq -r .NAMESPACE)
    POD_NAME=$(cat "${CONFIGFILE}" | jq -r .POD_NAME)
    CONT_NAME=$(cat "${CONFIGFILE}" | jq -r .CONT_NAME)
    PID=$(cat "${CONFIGFILE}" | jq -r .PID)

    if cat "${CONFIGFILE}" | jq -er .GOLANG > /dev/null 2>&1 ; then
        if [ "$(cat "${CONFIGFILE}" | jq -r .GOLANG)" == "yes" ]; then
            GOLANG="--golang"
        fi
    fi

    if cat "${CONFIGFILE}" | jq -er .PIDFILE > /dev/null 2>&1 ; then
        PIDFILE=$(cat "${CONFIGFILE}" | jq -r .PIDFILE)
    fi

    if cat "${CONFIGFILE}" | jq -er .PORTFILE > /dev/null 2>&1 ; then
        PORTFILE=$(cat "${CONFIGFILE}" | jq -r .PORTFILE)
    fi

    if cat "${CONFIGFILE}" | jq -er .LOGFILE > /dev/null 2>&1 ; then
        LOGFILE=$(cat "${CONFIGFILE}" | jq -r .LOGFILE)
    fi

    if cat "${CONFIGFILE}" | jq -er .START_TIMEOUT > /dev/null 2>&1 ; then
        START_TIMEOUT=$(cat "${CONFIGFILE}" | jq -r .START_TIMEOUT)
    fi
}

function_start_k8s_dbgserver () {
    if [ -f ${PIDFILE} ]; then
        echo "k8s-dbgserver is probably running, pidfile already exists"
        exit 1
    fi

    echo "Starting k8s-dbgserver"

    python3 k8s-dbgserver.py -n "${NAMESPACE}" "${POD_NAME}" -c "${CONT_NAME}" -p "${PID}" ${GOLANG} &> "${LOGFILE}" &
    echo "$!" > "${PIDFILE}"

    TRY=0

    while true ; do
        if ! kill -s 0 "$(cat ${PIDFILE})" > /dev/null 2>&1 ; then
            echo "k8s-dbgserver failed to start for some reason. See the logs:"
            cat "${LOGFILE}"

            rm -f "${PIDFILE}" "${PORTFILE}"

            exit 2
        fi
        PORT=$(grep "Port forwarded debugger is listening on localhost:" "${LOGFILE}" | sed 's#.* ##')

        if [ -z "${PORT}" ]; then
            if [ "${TRY}" -ge "${START_TIMEOUT}" ]; then
                echo "k8s-dbgserver did not started in time"

                function_stop_k8s_dbgserver

                exit 3
            fi
        else
            echo "${PORT}" > ${PORTFILE}
            echo "k8s-dbgserver is listening on ${PORT}"

            return
        fi

        echo "k8s-dbgserver did not started (yet)"
        TRY=$((TRY+1))
        sleep 1
    done
}

function_stop_k8s_dbgserver () {
    if [ ! -f ${PIDFILE} ]; then
        echo "k8s-dbgserver is not running"
        exit 1
    fi

    echo "Stopping k8s-dbgserver"
    kill -INT "$(cat ${PIDFILE})"

    TRY=0
    sleep 1

    while kill -s 0 "$(cat ${PIDFILE})" > /dev/null 2>&1 ; do
        if [ "${TRY}" -eq 5 ]; then
            echo "Sending sigkill to k8s-dbgserver"

            kill -9 "$(cat ${PIDFILE})"

            break
        fi

        echo "k8s-dbgserver still runs"
        TRY=$((TRY+1))
        sleep 1
    done

    rm -f "${PIDFILE}" "${PORTFILE}"
}

function_help () {
    echo "Usage: $0 { start | stop } [ CONFIGFILE ]"
}

if [ "$#" -eq 0 ]; then
    function_help
fi

ACTION=$1

if [ "$#" -eq 2 ]; then
    CONFIGFILE=$2
fi

function_get_config

if [ "${ACTION}" == "start" ]; then
    function_start_k8s_dbgserver
fi

if [ "${ACTION}" == "stop" ]; then
    function_stop_k8s_dbgserver
fi
