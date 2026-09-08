# Optional trust anchors for environments behind a TLS-inspecting proxy

Drop corporate root CA `.crt` files (PEM) here and they are installed into the
image's trust store at build time. Nothing here is required: on a normal
network this directory stays empty and the build is unaffected.

The `.crt` files themselves are gitignored because they are specific to a
particular corporate network, not to this project.

## Exporting the CA your proxy uses

The host machine and the container can be intercepted by *different* devices,
so check both. To export a CA from the Windows store:

```powershell
$c = Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root |
     Where-Object { $_.Subject -like '*<your proxy vendor>*' } |
     Select-Object -First 1
$b64 = [Convert]::ToBase64String($c.RawData, 'InsertLineBreaks')
"-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----" |
  Set-Content -Encoding ascii certs\corporate-ca.crt
```

The Dockerfile strips CR characters from these files automatically, because a
certificate exported on Windows has CRLF line endings and
`update-ca-certificates` silently ignores it while still reporting success and
exiting 0. The build then verifies that each certificate really is trusted and
fails loudly if it is not, rather than producing an image that cannot reach
PyPI or the OPUS API for reasons that only surface at runtime.

## When the CA itself is malformed

A root CA must carry `X509v3 Basic Constraints: critical, CA:TRUE`. If the
`critical` marking is missing, OpenSSL rejects it with:

```
certificate verify failed: Basic Constraints of CA cert not marked critical
```

Installing it in the trust store cannot fix this. The application cannot work
around it either, short of disabling certificate verification, which would
expose the OPUS credentials the dashboard transmits.

This was observed with a FortiGate SSL-inspection CA
(`O=Fortinet, CN=FG120GTK25046485`): its Basic Constraints are present but not
marked critical. An ESET SSL Filter CA on the same site was formed correctly,
which is why host tools worked while containers did not.

Inspect a certificate with:

```powershell
docker run --rm -v "${PWD}\certs:/certs:ro" python:3.13-slim `
  openssl x509 -in /certs/your-ca.crt -noout -text
```

If the CA is malformed, only the network team can resolve it. The usual
options are to reissue the inspection CA with Basic Constraints marked
critical, or to exempt the hosts this project needs from SSL inspection:

* `appsvc.opus4business.com` - the OPUS API the dashboard extracts from
* `pypi.org` and `files.pythonhosted.org` - required to build the image
* `ghcr.io` - required to pull the published image
