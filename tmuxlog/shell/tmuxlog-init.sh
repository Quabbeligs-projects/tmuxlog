if [ -n "${ZSH_VERSION-}" ]; then
  source "${${(%):-%x}:A:h}/tmuxlog.zsh"
elif [ -n "${BASH_VERSION-}" ]; then
  if [[ ${BASH_SOURCE[0]} == */* ]]; then
    source "${BASH_SOURCE[0]%/*}/tmuxlog.bash"
  else
    source ./tmuxlog.bash
  fi
fi
