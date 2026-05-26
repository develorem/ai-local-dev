# Sets the two Ollama env vars that unlock VRAM on a 16GB card:
#   OLLAMA_FLASH_ATTENTION = 1     (flash attention — much smaller activations)
#   OLLAMA_KV_CACHE_TYPE   = q8_0  (quantized KV cache — halves the per-token KV footprint)
#
# Both are set at User scope (persist across reboots). You must restart Ollama
# afterwards for them to take effect.

[Environment]::SetEnvironmentVariable("OLLAMA_FLASH_ATTENTION", "1", "User")
[Environment]::SetEnvironmentVariable("OLLAMA_KV_CACHE_TYPE",   "q8_0", "User")

Write-Output "Set user env vars:"
Write-Output ("  OLLAMA_FLASH_ATTENTION = " + [Environment]::GetEnvironmentVariable("OLLAMA_FLASH_ATTENTION", "User"))
Write-Output ("  OLLAMA_KV_CACHE_TYPE   = " + [Environment]::GetEnvironmentVariable("OLLAMA_KV_CACHE_TYPE", "User"))
Write-Output ""
Write-Output "Now restart Ollama for the changes to take effect:"
Write-Output "  1. Quit Ollama from the system tray (right-click the llama -> Quit)"
Write-Output "  2. Relaunch Ollama from the Start menu (or run: ollama serve)"
Write-Output "  3. Re-run the throughput matrix: py test-harness\bench.py run"
