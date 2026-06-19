## Python Windows Terminal Printing
When writing Python scripts that scrape the web or process LLM responses, NEVER directly `print()` raw dynamic strings. Windows PowerShell default encodings will throw a fatal `UnicodeEncodeError` when encountering characters like en-dashes or emojis.
*Fix:* Always encode/decode before printing:
```python
print(text.encode('ascii', 'replace').decode('ascii'))
```
