#!/usr/bin/env bash
#
# https://github.com/P3TERX/aria2.conf
# File name：clean.sh
# Description: Remove redundant files after Aria2 download is complete
# Version: 3.0 (verbatim upstream, MIT License)
#

CHECK_CORE_FILE() {
    CORE_FILE="$(dirname $0)/core"
    if [[ -f "${CORE_FILE}" ]]; then
        . "${CORE_FILE}"
    else
        echo "!!! core file does not exist !!!"
        exit 1
    fi
}

CHECK_CORE_FILE "$@"
CHECK_PARAMETER "$@"
CHECK_FILE_NUM
CHECK_SCRIPT_CONF
GET_TASK_INFO
GET_DOWNLOAD_DIR
CONVERSION_PATH
CLEAN_UP
exit 0
