# TriggerComplete

AI autocomplete for LibreOffice Writer with a local OpenAI-compatible model, fired only by a trigger phrase.

1. Type the trigger phrase (default `trigger phrase`). A streaming request starts in the background. Nothing is shown while it runs.
2. Press Tab as the very next key and whatever has arrived so far is inserted in place of the trigger phrase. Generation stops.
3. Press any other key, or click, and the completion is dropped and the request aborted. The key works as usual.

One Ctrl+Z undoes an accepted completion and brings the trigger phrase back.

## Install

Needs LibreOffice with Python support (Arch: included, Debian/Ubuntu: `libreoffice-script-provider-python`).

```
make install
```

This creates `settings.json` and `SYSTEM.md` from `settings.example.json` and `SYSTEM.example.md` if they are missing, builds `dist/TriggerComplete.oxt` and installs it with `unopkg` (close LibreOffice first). `make` alone only builds, and you can then add the .oxt in LibreOffice under Tools > Extension Manager > Add. `make uninstall` removes it. Without make: `python3 build.py --install`.

Adjust `settings.json` to your setup and write your instructions into `SYSTEM.md`. Neither is tracked by git, so personal settings and example texts stay local.

The build stores this directory's absolute path in the extension, and the plugin reads `settings.json` and `SYSTEM.md` from here. `--config-dir DIR` picks another directory, and the `TRIGGERCOMPLETE_DIR` environment variable overrides it at runtime. If the directory is missing, LibreOffice shows a warning at startup.

A `SYSTEM.md` with example texts can be a few thousand tokens, and up to 6000 characters of document go along with it, so give llama-server enough context, e.g. `llama-server -m model.gguf -c 8192`. llama-server caches the prompt, so only the first trigger pays for the long system prompt.

Both files are re-read when they change, so edits apply to the next trigger without restarting LibreOffice. The plugin logs to `triggercomplete.log` in the same directory, without document text.

## settings.json

| key | default | meaning |
|---|---|---|
| `trigger_phrase` | `trigger phrase` | text that starts a completion when typed |
| `trigger_case_sensitive` | `false` | off so that autocorrect capitalisation still matches |
| `remove_trigger_on_accept` | `true` | delete the trigger phrase on Tab and leave it out of the prompt |
| `api_base_url` | `http://127.0.0.1:8080/v1` | llama.cpp default. Ollama is `http://127.0.0.1:11434/v1` |
| `api_key` | empty | sent as a Bearer token if set |
| `api_mode` | `chat` | `chat` uses `/chat/completions` with SYSTEM.md as system message. `completions` uses `/completions` |
| `model` | `local-model` | model name sent to the server |
| `max_tokens`, `temperature`, `stop` | 200, 0.7, none | sampling |
| `thinking` | `false` | `false` asks reasoning models to skip thinking (chat mode only) by sending `thinking_off_body` |
| `thinking_off_body` | see below | fields merged into the request when `thinking` is `false` |
| `extra_body` | `{}` | merged into the request last, e.g. `{"top_k": 40}` |
| `request_timeout_seconds` | 120 | connect and idle read timeout |
| `context_chars_before` / `context_chars_after` | 3000 / 500 | document text sent around the cursor (same text frame or table cell) |
| `user_prompt_template` | see below | user message in chat mode |
| `completion_prompt_template` | `{before}` | prompt in completions mode. `{system}` is also available |
| `strip_think_tags` | `true` | hide a leading `<think>…</think>` block from reasoning models |
| `log_file`, `log_level` | `triggercomplete.log`, `info` | empty `log_file` turns logging off |
| `server_command` | empty | shell command that starts the LLM server, see below |
| `server_log_file` | `server.log` | output of `server_command`, in the config directory |

## Starting the server

With `server_command` set, e.g. `"CPU=1 SERVER=1 /path/to/run_llama_cpp.sh"`, the plugin runs it through `/bin/sh` when the first Writer document opens:

* It runs once per user. A second LibreOffice process uses the server that is already running.
* If something already listens on the port of `api_base_url`, the plugin assumes you started a server yourself and neither starts nor stops anything.
* It stops 5 seconds after the last Writer document in all LibreOffice processes has closed, and when LibreOffice exits. The whole process group gets SIGTERM, then SIGKILL after 5 seconds.
* The command must stay in the foreground until the server exits. A script that backgrounds or daemonizes the server cannot be stopped. End such a script with `exec llama-server ...`.
* If LibreOffice crashes, the server keeps running and the next LibreOffice adopts it.
* Requests sent while the model is still loading fail, and Tab does nothing. `server.log` shows the server output.

## Prompt

In chat mode the model gets `SYSTEM.md` as the system message and this user message, with up to `context_chars_before` characters before the cursor (trigger phrase removed) and `context_chars_after` after it:

```
<document>
...text before<CURSOR>text after...
</document>

Write the text to insert at <CURSOR>.
```

The server applies the model's chat template, so this suits instruction-tuned models. `api_mode: "completions"` is for raw base models.

## Thinking

With `"thinking": false` each chat request carries `{"chat_template_kwargs": {"enable_thinking": false}}`. llama.cpp passes it to the model's chat template, which works for Qwen3, GLM and similar templates. Some templates use another name, e.g. DeepSeek V3.1 wants `{"chat_template_kwargs": {"thinking": false}}`, so adjust `thinking_off_body` for your model. Ollama ignores this field and needs `{"reasoning_effort": "none"}` instead. Thinking that slips through anyway is still hidden by `strip_think_tags`.

## Behaviour details

* Only typed characters fire the trigger. Moving onto an existing trigger phrase with arrows or the mouse, or pasting it, does not.
* Tab before any text has arrived inserts nothing and leaves the trigger phrase in place. A failed request behaves the same way, and the error is logged.
* If the cursor is no longer right after the trigger phrase when Tab is pressed, Tab works as a normal Tab.
* Newlines in the completion become paragraph breaks. A leading space is dropped if the document already has one before the cursor.
* Aborting closes the HTTP connection. llama.cpp and Ollama stop generating when a streaming client disconnects.

## Tests

```
make test    # unit tests: core logic and server management, against the mock server
make e2e     # real Writer on Xvfb, keys typed with xdotool
make mock    # mock server on :8080 for manual testing
```

The end-to-end test needs `soffice`, `unopkg`, `Xvfb`, `xdotool` and the `uno` Python module, and must run as a normal user.

## License

MIT, see [LICENSE](LICENSE).
