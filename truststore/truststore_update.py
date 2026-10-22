#!/usr/bin/env python3
"""
truststore_update.py - check the CA bundle (PEM) of a CloudFront trust store that you
keep in Git, list what it changes compared with the bundle in S3, and on "yes" upload
it and update the trust store.

  python3 truststore_update.py truststore/prod/ca-bundle.pem

Settings come from truststore.json in the same folder as the PEM file (or --config):

  {
    "account_id": "111122223333",
    "trust_store": "ims-mtls-prod",
    "bucket": "ims-mtls-truststore-prod",
    "key": "ca-bundle.pem",
    "region": "eu-west-2"
  }

  trust_store is the trust store's name, ID or ARN; bucket, key and region say where
  its bundle is in S3. CloudFront does not report which S3 file a trust store was built
  from, so this file is what ties the two together. One folder per trust store.

What it does
 1. Checks your file against CloudFront's rules for trust store bundles: only
    CERTIFICATE blocks, comments start with # and contain no "-", no blank lines,
    X.509 v3, RSA 2048/3072/4096 or ECDSA P-256/P-384 keys, SHA-256/384/512
    signatures, at most 25 certificates and 1 MB. It also refuses private keys, CSRs,
    duplicates, and certificates you add that are expired or are not CA certificates
    (a client's own certificate). Certificates already in the trust store only get
    warnings, as do certificates that expire within 30 days and a file with
    uncommitted Git changes.