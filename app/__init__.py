"""MerchantOps Agent.

The integrity check runs at package import, before any module below it is
loaded, because the thing it guards against is a rewritten module below it. See
`app/integrity.py` — a mutation-test run that was killed can leave a disabled
safety control on disk, and every entrypoint into this application goes through
this import.
"""
from app.integrity import check as _check

_check()
