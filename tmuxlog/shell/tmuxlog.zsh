# tmuxlog zsh hooks (zsh >= 5.1). Sourced by tmuxlog-init.sh; do not execute.
#
# Per command the hooks do no forks: one append to <pane>/hooks.rec through a
# file descriptor opened once, and one OSC mark written to the tty. Any write
# failure disarms the hooks; the user's command is never blocked or altered.
# Record format and marks are documented in docs/DESIGN.md §2 and §4.

[[ -o interactive ]] || return 0
[[ -z ${__tl_loaded-} ]] || return 0
zmodload zsh/datetime 2>/dev/null || return 0
autoload -Uz add-zsh-hook || return 0
typeset -g __tl_loaded=1

typeset -g __tl_root=${TMUXLOG_ROOT:-$HOME/.tmuxlog}
typeset -g __tl_nonce="z$$r$RANDOM$RANDOM"
typeset -g __tl_fd=-1 __tl_tty=-1 __tl_dir= __tl_broken= __tl_seq=0 __tl_open=0 __tl_ts=0
typeset -g __tl_bmark=$'\e]6973;tmuxlog;B;'"$__tl_nonce"$'\a'
typeset -g __tl_psmark="%{$__tl_bmark%}"

# $epochtime is (seconds nanoseconds) as integers: true ns, no locale radix.
# Read once so both halves come from the same clock sample.
__tl_now() {
    local -a t
    t=($epochtime)
    __tl_ts=$t[1]${(l:9::0:)t[2]}
}

__tl_mark() {
    (( __tl_tty >= 0 )) && printf '\e]6973;tmuxlog;%s\a' "$1" >&$__tl_tty 2>/dev/null
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
        [[ -r $ptr ]] && IFS= read -r REPLY <$ptr
    fi
    return 0
}

__tl_disarm() {
    (( __tl_fd >= 0 )) && exec {__tl_fd}>&-
    (( __tl_tty >= 0 )) && exec {__tl_tty}>&-
    __tl_fd=-1 __tl_tty=-1 __tl_dir= __tl_open=0
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
    # One fork per arm (not per command) so the file gets 0600 regardless of the user's umask.
    (umask 077 && : >>$dir/hooks.rec) 2>/dev/null || { __tl_broken=$dir; return 0 }
    { exec {__tl_fd}>>$dir/hooks.rec } 2>/dev/null || { __tl_fd=-1 __tl_broken=$dir; return 0 }
    { exec {__tl_tty}>/dev/tty } 2>/dev/null || __tl_tty=-1
    __tl_dir=$dir
    __tl_now
    printf 'I %s %s %d zsh %s %d\n' $__tl_nonce $__tl_ts $$ $ZSH_VERSION ${SHLVL:-0} \
        >&$__tl_fd 2>/dev/null || { __tl_fail; return 0 }
    __tl_mark "I;$__tl_nonce"
    return 0
}

# $1 is the line as typed (multi-line, heredoc body and leading space
# included, even under HIST_IGNORE_SPACE); $3 is the full text being executed,
# used when the history mechanism is off and $1 is empty.
__tl_preexec() {
    emulate -L zsh
    setopt no_multibyte
    local LC_ALL=C
    (( __tl_fd >= 0 )) || return 0
    __tl_now
    local cmd=$1 src=typed
    [[ -n $cmd ]] || { cmd=$3; src=expanded }
    (( ++__tl_seq ))
    __tl_open=1
    printf 'S %d %s %s %d %s %d:%s %d:%s %d:%s %d:%s\n' $__tl_seq $__tl_ts $__tl_nonce ${SHLVL:-0} $src \
        ${#cmd} "$cmd" ${#PWD} "$PWD" ${#USER} "${USER-}" ${#HOST} "${HOST-}" \
        >&$__tl_fd 2>/dev/null || { __tl_fail; return 0 }
    __tl_mark "C;$__tl_nonce;$__tl_seq"
    return 0
}

# First precmd function: seals the finished command, re-checks arming.
# zsh hands every precmd function the command's $?, so returning 0 is safe.
__tl_precmd() {
    local ec=$?
    emulate -L zsh
    setopt no_multibyte
    local LC_ALL=C
    if (( __tl_open )); then
        __tl_open=0
        __tl_now
        printf 'E %d %s %s %d\n' $__tl_seq $__tl_ts $__tl_nonce $ec >&$__tl_fd 2>/dev/null || __tl_fail
        __tl_mark "D;$__tl_nonce;$__tl_seq;$ec"
    fi
    __tl_rearm
    __tl_mark "A;$__tl_nonce"
    return 0
}

# Last precmd function: re-appends the prompt-end mark in case a prompt
# framework rebuilt PS1.
__tl_arm() {
    (( __tl_fd >= 0 )) || return 0
    [[ $PS1 == *"$__tl_psmark"* ]] || PS1+=$__tl_psmark
    return 0
}

precmd_functions=(__tl_precmd ${precmd_functions:#__tl_precmd})
add-zsh-hook precmd __tl_arm
add-zsh-hook preexec __tl_preexec
