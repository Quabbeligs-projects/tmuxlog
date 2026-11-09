[[ $- == *i* ]] || return 0
(( BASH_VERSINFO[0] >= 5 )) || return 0
[[ -n ${__tl_loaded-} ]] && return 0
__tl_loaded=1

__tl_root=${TMUXLOG_ROOT:-$HOME/.tmuxlog}
__tl_nonce="b$$r$RANDOM$RANDOM"
__tl_fd=-1
__tl_tty=-1
__tl_dir=
__tl_broken=
__tl_seq=0
__tl_open=0
__tl_at_prompt=0
__tl_hnum=
__tl_ec=0
__tl_ts=0
__tl_src=
__tl_npart=0
__tl_style=osc
__tl_psmark=
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
  if (( __tl_tty < 0 )); then return 0; fi
  __tl_markstr "$1"
  printf '%s' "$REPLY" >&"$__tl_tty" 2>/dev/null
  return 0
}

__tl_pane_dir() {
  REPLY=
  if [[ -n ${TMUXLOG_PANE_DIR-} ]]; then
    REPLY=$TMUXLOG_PANE_DIR
    return 0
  fi
  [[ -z ${TMUX-} || -z ${TMUX_PANE-} ]] && return 0
  local spid=${TMUX#*,}
  spid=${spid%%,*}
  local f=$__tl_root/run/$spid/${TMUX_PANE#%}
  if [[ -r $f ]]; then
    IFS= read -r REPLY <"$f"
  fi
  return 0
}

__tl_disarm() {
  if (( __tl_fd >= 0 )); then exec {__tl_fd}>&-; fi
  if (( __tl_tty >= 0 )); then exec {__tl_tty}>&-; fi
  __tl_fd=-1
  __tl_tty=-1
  __tl_dir=
  __tl_open=0
  __tl_at_prompt=0
  if [[ -n $__tl_psmark ]]; then
    PS1=${PS1//"$__tl_psmark"/}
  fi
  return 0
}

__tl_fail() {
  __tl_broken=$__tl_dir
  __tl_disarm
}

__tl_rearm() {
  __tl_pane_dir
  local d=$REPLY
  [[ -d $d ]] || d=
  [[ $d == "$__tl_dir" ]] && return 0
  if [[ -n $d && $d == "$__tl_broken" ]]; then
    return 0
  fi
  __tl_disarm
  __tl_broken=
  [[ -z $d ]] && return 0

  if ! (umask 077 && : >>"$d/hooks.rec" && : >>"$d/.lastcmd") 2>/dev/null; then
    __tl_broken=$d
    return 0
  fi
  if ! { exec {__tl_fd}>>"$d/hooks.rec"; } 2>/dev/null; then
    __tl_fd=-1
    __tl_broken=$d
    return 0
  fi
  { exec {__tl_tty}>/dev/tty; } 2>/dev/null || __tl_tty=-1
  __tl_dir=$d

  __tl_style=osc
  if [[ -r $d/boundary ]]; then
    IFS= read -r REPLY <"$d/boundary"
    if [[ $REPLY == sentinel ]]; then __tl_style=sentinel; fi
  fi
  __tl_markstr "B;$__tl_nonce"
  __tl_psmark="\[$REPLY\]"

  __tl_now
  if ! printf 'I %s %s %d bash %s %d\n' "$__tl_nonce" "$__tl_ts" "$$" "${BASH_VERSION// /_}" "${SHLVL:-0}" >&"$__tl_fd" 2>/dev/null; then
    __tl_fail
    return 0
  fi
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
  if [[ -z $cmd ]]; then
    cmd=$BASH_COMMAND
    src=bc
  fi
  __tl_seq=$(( __tl_seq + 1 ))
  __tl_open=1
  __tl_src=$src
  __tl_npart=0
  printf 'S %d %s %s %d %s %d:%s %d:%s %d:%s %d:%s\n' \
    "$__tl_seq" "$__tl_ts" "$__tl_nonce" "${SHLVL:-0}" "$src" \
    "${#cmd}" "$cmd" "${#PWD}" "$PWD" "${#USER}" "${USER-}" "${#HOSTNAME}" "${HOSTNAME-}" \
    >&"$__tl_fd" 2>/dev/null || { __tl_fail; return 0; }
  __tl_mark "C;$__tl_nonce;$__tl_seq"
  return 0
}

__tl_debug() {
  if [[ -n ${COMP_LINE+x} || -n ${READLINE_POINT+x} ]]; then
    return 0
  fi
  case $BASH_COMMAND in
    __tl_*) return 0 ;;
  esac

  if (( __tl_at_prompt )); then
    __tl_at_prompt=0
    (( __tl_fd >= 0 )) || __tl_rearm
    (( __tl_fd >= 0 )) && __tl_preexec
    return 0
  fi
  (( __tl_fd < 0 )) && return 0
  (( __tl_open )) || return 0
  [[ $__tl_src == bc ]] || return 0
  (( __tl_npart < __tl_max_part )) || return 0

  __tl_npart=$(( __tl_npart + 1 ))
  local LC_ALL=C c=$BASH_COMMAND
  __tl_now
  printf 'T %d %s %s %d %d:%s\n' "$__tl_seq" "$__tl_ts" "$__tl_nonce" "$__tl_npart" "${#c}" "$c" >&"$__tl_fd" 2>/dev/null || __tl_fail
  return 0
}

__tl_precmd() {
  __tl_ec=$?
  __tl_at_prompt=0
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
  if (( __tl_fd >= 0 )) && [[ $PS1 != *"$__tl_psmark"* ]]; then
    PS1+=$__tl_psmark
  fi
  __tl_at_prompt=1
  return $__tl_ec
}

__tl_trap_done=

__tl_install_trap() {
  local old=$1
  __tl_trap_done=1
  if [[ -z $old ]]; then
    trap __tl_debug DEBUG
    return 0
  fi
  [[ $old == *__tl_debug* ]] && return 0
  eval "local -a parts=($old)"
  trap "__tl_debug; ${parts[2]}" DEBUG
}

__tl_trap_line='[[ -n $__tl_trap_done ]] || __tl_install_trap "$(trap -p DEBUG)"'

if [[ ${PROMPT_COMMAND@a} == *a* ]]; then
  PROMPT_COMMAND=(__tl_precmd "${PROMPT_COMMAND[@]}" "$__tl_trap_line" __tl_arm)
else
  PROMPT_COMMAND=$'__tl_precmd\n'"${PROMPT_COMMAND-}"$'\n'"$__tl_trap_line"$'\n__tl_arm'
fi
unset __tl_trap_line
