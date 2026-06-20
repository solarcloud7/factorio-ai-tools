## Python Windows Terminal Printing
When writing Python scripts that scrape the web or process LLM responses, NEVER directly `print()` raw dynamic strings. Windows PowerShell default encodings will throw a fatal `UnicodeEncodeError` when encountering characters like en-dashes or emojis.
*Fix:* Always encode/decode before printing:
```python
print(text.encode('ascii', 'replace').decode('ascii'))
```

## Comprehensive Git Commits
When tasked with committing and pushing code to a repository, NEVER assume you have tracked all necessary files based on memory. 
- Before confirming to the user that "all changes have been pushed", you MUST run `git status` to explicitly verify that no unexpectedly modified or untracked files were left behind in the working directory.
- Ensure that you either use `git add .` (if safe) or meticulously stage every modified file related to the current task before committing.
