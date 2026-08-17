"""Helpers with no knowledge of any protocol, safe to import anywhere.

The protocol-agnostic tier described in :mod:`async_gateway.helpers`.
A module belongs here only when it would still make sense in a library
that spoke none of HTTP, SOAP, FTP or SFTP -- today that is
:mod:`~async_gateway.helpers.common.date_helper`, which supplies the
wall-clock timestamp and the monotonic duration every envelope carries.

The bar is deliberately strict, because this tier's value is that
importing from it can never create a cycle. A helper that needs the
request contract belongs in ``helpers/internal/`` instead.

This module intentionally exports nothing itself: importers name the
submodule they want.
"""
