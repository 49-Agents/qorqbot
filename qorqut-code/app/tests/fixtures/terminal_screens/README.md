# Terminal screen and transcript captures

Real tmux captures (`capture-pane -p -e`, with the `pane_in_mode|pane_dead|cursor_flag|cursor_x|cursor_y` line in the
matching `.cursor` file) and one transcript excerpt, taken from the installed clients on 2026-10-02: Claude Code 2.1.287
and codex-cli 0.159.3. Each client ran in an isolated tmux socket with its own home directory against a local mock model
server on 127.0.0.1: no production agent, account or real model was involved. The conversation text is probe filler.

The `running`, `queued-*`, `typed-while-running` and `pending-steer` captures were taken while a turn was in progress
(an ordinary Enter had just queued or steered the previous message). `claude-2.1.287-folded-queue.jsonl` is the Claude
transcript of two messages sent while a turn ran: they were folded into the turn as `queued_command` attachments and have
no `user` record.
