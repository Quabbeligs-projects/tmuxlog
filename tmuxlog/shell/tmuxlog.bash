# tmuxlog bash hooks (bash >= 5.0). Sourced by tmuxlog-init.sh; do not execute.
#
# Per command the hooks do no forks: one append to <pane>/hooks.rec through a
# file descriptor opened once, and one OSC mark written to the tty. Any write
# failure disarms the hooks; the user's command is never blocked or altered.
# Record format and marks are documented in docs/DESIGN.md §2 and §4.

[[ $- == *i* ]] || return 0
(( BASH_VERSINFO[0] >= 5 )) || return 0
[[ -z ${__tl_loaded-} ]] || return 0
__tl_loaded=1

__tl_root=${TMUXLOG_ROOT:-$HOME/.tmuxlog}
__tl_nonce="b$$r$RANDOM$RANDOM"
__tl_fd=-1 __tl_tty=-1 __tl_dir= __tl_broken= __tl_seq=0 __tl_open=0
__tl_at_prompt=0 __tl_hnum= __tl_ec=0 __tl_ts=0
__tl_bmark=$'\e]6973;tmuxlog;B;'"$__tl_nonce"$'\a'
__tl_psmark="\[$__tl_bmark\]"

# Callers hold `local LC_ALL=C`, so the radix is '.'; stripping every non-digit
# also makes the parse independent of whatever radix the locale would use.
# bash's clock has microsecond resolution: ns = us * 1000.
__tl_now() {
    local t=${EPOCHREALTIME//[!0-9]/}
    __tl_ts=${t}000
}

__tl_mark() {
    (( __tl_tty >= 0 )) && printf '\e]6973;tmuxlog;%s\a' "$1" >&"$__tl_tty" 2>/dev/null
    return 0
}

__tl_pane_dir() {
    REPLY=
    if [[ -n ${TMUXLOG_PANE_DIR-} ]]; then
        REPLY=$TMUXLOG_PANE_DIR
    elif [[ -n ${TMUX-} && -n ${TMUX_PANE-} ]]; then
        # $TMUX is "<socket>,<server pid>,<session index>".
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
    PS1=${PS1//"$__tl_psmark"/}
    return 0
}

# Write failed: disarm and do not retry this directory until the pointer changes.
__tl_fail() {
    __tl_broken=$__tl_dir
    __tl_disarm
}

# Runs at every prompt: follows the arm pointer so start-session/stop-session
# take effect in shells that were already running.
__tl_rearm() {
    __tl_pane_dir
    local dir=$REPLY
    [[ -n $dir && -d $dir ]] || dir=
    [[ $dir == "$__tl_dir" ]] && return 0
    [[ -n $dir && $dir == "$__tl_broken" ]] && return 0
    __tl_disarm
    __tl_broken=
    [[ -n $dir ]] || return 0
    # One fork per arm (not per command) so files get 0600 regardless of the user's umask.
    (umask 077 && : >>"$dir/hooks.rec" && : >>"$dir/.lastcmd") 2>/dev/null || { __tl_broken=$dir; return 0; }
    { exec {__tl_fd}>>"$dir/hooks.rec"; } 2>/dev/null || { __tl_fd=-1 __tl_broken=$dir; return 0; }
    { exec {__tl_tty}>/dev/tty; } 2>/dev/null || __tl_tty=-1
    __tl_dir=$dir
    __tl_now
    printf 'I %s %s %d bash\n' "$__tl_nonce" "$__tl_ts" "$$" >&"$__tl_fd" 2>/dev/null || { __tl_fail; return 0; }
    __tl_mark "I;$__tl_nonce"
    return 0
}

__tl_preexec() {
    local LC_ALL=C cmd=
    __tl_now
    # HISTCMD in the DEBUG trap equals its value at the previous prompt only if
    # this line was saved to history (not dropped by ignorespace/ignoredups or
    # `set +o history`). Only then is `fc -ln -1` the current line; it is used
    # to recover the full pipeline / multi-line / heredoc text.
    if [[ -o history && -n ${HISTCMD-} && $HISTCMD == "$__tl_hnum" ]]; then
        fc -ln -1 >|"$__tl_dir/.lastcmd" 2>/dev/null
        IFS= read -r -d '' cmd <"$__tl_dir/.lastcmd" 2>/dev/null
        cmd=${cmd#$'\t'}
        cmd=${cmd# }
        cmd=${cmd%$'\n'}
    fi
    # Primary, always-available path: the command about to run. For a line
    # that history did not keep, this is only its first simple command
    # (`a | b` gives `a`); heredoc bodies are included.
    [[ -n $cmd ]] || cmd=$BASH_COMMAND
    (( ++__tl_seq ))
    __tl_open=1
    printf 'S %d %s %s %d %d:%s %d:%s %d:%s %d:%s\n' "$__tl_seq" "$__tl_ts" "$__tl_nonce" "${SHLVL:-0}" \
        "${#cmd}" "$cmd" "${#PWD}" "$PWD" "${#USER}" "${USER-}" "${#HOSTNAME}" "${HOSTNAME-}" \
        >&"$__tl_fd" 2>/dev/null || { __tl_fail; return 0; }
    __tl_mark "C;$__tl_nonce;$__tl_seq"
    return 0
}

# DEBUG trap. Must always return 0: under `shopt -s extdebug` a non-zero
# return would skip the user's command.
__tl_debug() {
    (( __tl_fd >= 0 )) || return 0
    # Programmable completion and `bind -x` widgets run at the prompt.
    [[ -n ${COMP_LINE+x} || -n ${READLINE_POINT+x} ]] && return 0
    case $BASH_COMMAND in __tl_*) return 0 ;; esac
    if (( __tl_at_prompt )); then
        __tl_at_prompt=0
        __tl_preexec
    fi
    return 0
}

# First PROMPT_COMMAND entry: seals the finished command, re-checks arming.
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

# Last PROMPT_COMMAND entry: anything the DEBUG trap sees after this is the
# user's next command. Re-appends the prompt-end mark in case a prompt
# framework rebuilt PS1. Returns the command's status so PS1 can still show $?.
__tl_arm() {
    if (( __tl_fd >= 0 )); then
        [[ $PS1 == *"$__tl_psmark"* ]] || PS1+=$__tl_psmark
        __tl_at_prompt=1
    fi
    return $__tl_ec
}

if [[ ${PROMPT_COMMAND@a} == *a* ]]; then
    PROMPT_COMMAND=(__tl_precmd "${PROMPT_COMMAND[@]}" __tl_arm)
else
    PROMPT_COMMAND=$'__tl_precmd\n'"${PROMPT_COMMAND-}"$'\n__tl_arm'
fi

# Chain an existing DEBUG trap instead of replacing it.
__tl_prev_trap=$(trap -p DEBUG)
if [[ -n $__tl_prev_trap ]]; then
    eval "__tl_prev_trap=(${__tl_prev_trap})"
    trap "__tl_debug; ${__tl_prev_trap[2]}" DEBUG
else
    trap __tl_debug DEBUG
fi
unset __tl_prev_trap
