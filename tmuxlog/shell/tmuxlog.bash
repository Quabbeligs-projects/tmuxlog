[[ $- == *i* ]] || return 0
(( BASH_VERSINFO[0] >= 5 )) || return 0
[[ -z ${__tl_loaded-} ]] || return 0
__tl_loaded=1

__tl_root=${TMUXLOG_ROOT:-$HOME/.tmuxlog}
__tl_nonce="b$$r$RANDOM$RANDOM"
__tl_fd=-1 __tl_tty=-1 __tl_dir= __tl_broken= __tl_seq=0 __tl_open=0
__tl_at_prompt=0 __tl_hnum= __tl_ec=0 __tl_ts=0 __tl_src= __tl_npart=0
__tl_style=osc __tl_bmark= __tl_psmark=
__tl_max_part=${TMUXLOG_MAX_PARTS:-32}

__tl_now() {
    local t=${EPOCHREALTIME//[!0-9]/}
    __tl_ts=${t}000
}

__tl_markstr() {
    if [[ $__tl_style == sentinel ]]; then
        local s="@@TMUXLOG;$1@@"
        REPLY=$s$'\e['"${#s}"$'D\e[K'
    else
        REPLY=$'\e]6973;tmuxlog;'"$1"$'\a'
    fi
}

__tl_mark() {
    (( __tl_tty >= 0 )) || return 0
    __tl_markstr "$1"
    printf '%s' "$REPLY" >&"$__tl_tty" 2>/dev/null
    return 0
}

__tl_pane_dir() {
    REPLY=
    if [[ -n ${TMUXLOG_PANE_DIR-} ]]; then
        REPLY=$TMUXLOG_PANE_DIR
    elif [[ -n ${TMUX-} && -n ${TMUX_PANE-} ]]; then
        local pid=${TMUX#*,}
        pid=${pid%%,*}
        local ptr=$__tl_root/run/$pid/${TMUX_PANE#%}
        [[ -r $ptr ]] && IFS= read -r REPLY <"$ptr"
    fi
    return 0
}

__tl_disarm() {
    (( __tl_fd >= 0 )) && exec {__tl_fd}>&-
    (( __tl_tty >= 0 )) && exec {__tl_tty}>&-
    __tl_fd=-1 __tl_tty=-1 __tl_dir= __tl_open=0 __tl_at_prompt=0
    [[ -n $__tl_psmark ]] && PS1=${PS1//"$__tl_psmark"/}
    return 0
}

__tl_fail() {
    __tl_broken=$__tl_dir
    __tl_disarm
}

__tl_rearm() {
    __tl_pane_dir
    local dir=$REPLY
    [[ -n $dir && -d $dir ]] || dir=
    [[ $dir == "$__tl_dir" ]] && return 0
    [[ -n $dir && $dir == "$__tl_broken" ]] && return 0
    __tl_disarm
    __tl_broken=
    [[ -n $dir ]] || return 0
    (umask 077 && : >>"$dir/hooks.rec" && : >>"$dir/.lastcmd") 2>/dev/null || { __tl_broken=$dir; return 0; }
    { exec {__tl_fd}>>"$dir/hooks.rec"; } 2>/dev/null || { __tl_fd=-1 __tl_broken=$dir; return 0; }
    { exec {__tl_tty}>/dev/tty; } 2>/dev/null || __tl_tty=-1
    __tl_dir=$dir
    __tl_style=osc
    if [[ -r $dir/boundary ]]; then
        IFS= read -r REPLY <"$dir/boundary"
        [[ $REPLY == sentinel ]] && __tl_style=sentinel
    fi
    __tl_markstr "B;$__tl_nonce"
    __tl_bmark=$REPLY __tl_psmark="\[$REPLY\]"
    __tl_now
    printf 'I %s %s %d bash %s %d\n' "$__tl_nonce" "$__tl_ts" "$$" "${BASH_VERSION// /_}" "${SHLVL:-0}" \
        >&"$__tl_fd" 2>/dev/null || { __tl_fail; return 0; }
    __tl_mark "I;$__tl_nonce"
    return 0
}

__tl_preexec() {
    local LC_ALL=C cmd= src=hist
    __tl_now
    if [[ -o history && -n ${HISTCMD-} && $HISTCMD == "$__tl_hnum" ]]; then
        fc -ln -1 >|"$__tl_dir/.lastcmd" 2>/dev/null
        IFS= read -r -d '' cmd <"$__tl_dir/.lastcmd" 2>/dev/null
        cmd=${cmd#$'\t'}
        cmd=${cmd# }
        cmd=${cmd%$'\n'}
    fi
    [[ -n $cmd ]] || cmd=$BASH_COMMAND src=bc
    (( ++__tl_seq ))
    __tl_open=1 __tl_src=$src __tl_npart=0
    printf 'S %d %s %s %d %s %d:%s %d:%s %d:%s %d:%s\n' "$__tl_seq" "$__tl_ts" "$__tl_nonce" "${SHLVL:-0}" "$src" \
        "${#cmd}" "$cmd" "${#PWD}" "$PWD" "${#USER}" "${USER-}" "${#HOSTNAME}" "${HOSTNAME-}" \
        >&"$__tl_fd" 2>/dev/null || { __tl_fail; return 0; }
    __tl_mark "C;$__tl_nonce;$__tl_seq"
    return 0
}

__tl_debug() {
    [[ -n ${COMP_LINE+x} || -n ${READLINE_POINT+x} ]] && return 0
    case $BASH_COMMAND in __tl_*) return 0 ;; esac
    if (( __tl_at_prompt )); then
        __tl_at_prompt=0
        (( __tl_fd >= 0 )) || __tl_rearm
        (( __tl_fd >= 0 )) && __tl_preexec
    elif (( __tl_fd < 0 )); then
        :
    elif (( __tl_open && __tl_npart < __tl_max_part )) && [[ $__tl_src == bc ]]; then
        (( ++__tl_npart ))
        local LC_ALL=C c=$BASH_COMMAND
        __tl_now
        printf 'T %d %s %s %d %d:%s\n' "$__tl_seq" "$__tl_ts" "$__tl_nonce" "$__tl_npart" "${#c}" "$c" \
            >&"$__tl_fd" 2>/dev/null || __tl_fail
    fi
    return 0
}

__tl_precmd() {
    __tl_ec=$?
    local LC_ALL=C
    if (( __tl_open )); then
        __tl_open=0
        __tl_now
        printf 'E %d %s %s %d\n' "$__tl_seq" "$__tl_ts" "$__tl_nonce" "$__tl_ec" >&"$__tl_fd" 2>/dev/null || __tl_fail
        __tl_mark "D;$__tl_nonce;$__tl_seq;$__tl_ec"
    fi
    __tl_rearm
    __tl_mark "A;$__tl_nonce"
    __tl_hnum=${HISTCMD-}
    return $__tl_ec
}

__tl_arm() {
    (( __tl_fd >= 0 )) && [[ $PS1 != *"$__tl_psmark"* ]] && PS1+=$__tl_psmark
    __tl_at_prompt=1
    return $__tl_ec
}

if [[ ${PROMPT_COMMAND@a} == *a* ]]; then
    PROMPT_COMMAND=(__tl_precmd "${PROMPT_COMMAND[@]}" __tl_arm)
else
    PROMPT_COMMAND=$'__tl_precmd\n'"${PROMPT_COMMAND-}"$'\n__tl_arm'
fi

__tl_prev_trap=$(trap -p DEBUG)
if [[ -n $__tl_prev_trap ]]; then
    eval "__tl_prev_trap=(${__tl_prev_trap})"
    trap "__tl_debug; ${__tl_prev_trap[2]}" DEBUG
else
    trap __tl_debug DEBUG
fi
unset __tl_prev_trap
