if [ -n "${ZSH_VERSION-}" ]; then
    . "${${(%):-%x}:A:h}/tmuxlog.zsh"
elif [ -n "${BASH_VERSION-}" ]; then
    if [[ ${BASH_SOURCE[0]} == */* ]]; then
        . "${BASH_SOURCE[0]%/*}/tmuxlog.bash"
    else
        . ./tmuxlog.bash
    fi
fi
