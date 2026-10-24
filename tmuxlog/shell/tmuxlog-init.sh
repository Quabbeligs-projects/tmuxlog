# tmuxlog shell integration for bash (>= 5.0) and zsh.
#
# Add ONE line to the end of your ~/.bashrc or ~/.zshrc (tmuxlog never edits them):
#     source "$(tmuxlog init-script)"
# or source this file by its absolute path.
#
# The hooks stay inert until `tmuxlog start-session` arms the pane, and are
# disarmed again by `tmuxlog stop-session`; no restart of the shell is needed.

if [ -n "${ZSH_VERSION-}" ]; then
    . "${${(%):-%x}:A:h}/tmuxlog.zsh"
elif [ -n "${BASH_VERSION-}" ]; then
    if [[ ${BASH_SOURCE[0]} == */* ]]; then
        . "${BASH_SOURCE[0]%/*}/tmuxlog.bash"
    else
        . ./tmuxlog.bash
    fi
fi
