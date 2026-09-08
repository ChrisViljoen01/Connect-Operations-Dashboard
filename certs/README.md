# Optional trust anchors for build environments behind a TLS-inspecting proxy.
# Drop corporate root CA .crt files (PEM) here and they are installed into the
# image's trust store at build time. Nothing here is required: on a normal
# network this directory stays empty and the build is unaffected.
#
# The .crt files themselves are gitignored because they are specific to a
# particular corporate network, not to this project.
#
# To export the CA your proxy uses, on Windows PowerShell:
#
#   $c = Get-ChildItem Cert:\LocalMachine\Root |
#        Where-Object { $_.Subject -like '*<your proxy vendor>*' } |
#        Select-Object -First 1
#   $b64 = [Convert]::ToBase64String($c.RawData, 'InsertLineBreaks')
#   "-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----" |
#     Set-Content -Encoding ascii certs\corporate-ca.crt
