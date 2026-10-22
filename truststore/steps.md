I'll build a script that checks your edited PEM file and compares it with the bundle the trust store uses now. It then asks for yes, uploads the file to S3 and updates the trust store. First I'm checking the CloudFront trust store API and its certificate rules.

Two of the seven deliberate bugs went unnoticed: the S3 read-back check, and the wording for a non-CA certificate that's already in the trust store. I'm adding tests for both.

The suite now catches all 15 deliberate bugs. Next I'm reading the full script once more for wording and edge cases.

All tests pass, and both safety-check runs are clean. Last, I'm reading the top of the script, since that's the help text you'll see.

I've written `truststore_update.py`. You give it the path to your edited PEM file. It checks the file against CloudFront's rules and compares it with the file in S3, listing what was added, removed or replaced. If you type yes, it uploads your file and updates the trust store. It needs only Python and boto3, so it runs in CloudShell as is.

**Setup.** The trust store API doesn't say which S3 file it was built from. So each environment folder needs a small `truststore.json` next to its PEM file:

```
truststore/
  prod/  ca-bundle.pem  truststore.json
  test/  ca-bundle.pem  truststore.json
  ppe/   ca-bundle.pem  truststore.json
```
```json
{
  "account_id": "111122223333",
  "trust_store": "ims-mtls-prod",
  "bucket": "ims-mtls-truststore-prod",
  "key": "ca-bundle.pem",
  "region": "eu-west-2"
}
```

`trust_store` can be the name, ID or ARN. For the first commit in each folder, copy the PEM file that's in S3 today (`aws s3 cp s3://<bucket>/<key> truststore/prod/ca-bundle.pem`).

**Running it:**
```bash
python3 truststore_update.py truststore/prod/ca-bundle.pem               # check, compare, ask, apply
python3 truststore_update.py truststore/prod/ca-bundle.pem --diff-only   # check and compare only
python3 truststore_update.py truststore/prod/ca-bundle.pem --validate-only   # no AWS needed
```

**Simulated run.** Your file adds one CA, removes one and replaces one renewed CA (fingerprints shortened):
```
Trust store ims-mtls-prod (ts-4f1c2a9be0d7) in account 111122223333: active, 6 CA certificates, OCSP checking on
  Last updated 2026-10-01 10:15 UTC; used by 2 distribution(s): E1Q2W3E4R5T6Y7 (api.tescoims.example), ...
  Bundle in S3: s3://ims-mtls-truststore-prod/mtls/ca-bundle.pem, version 3HL4…v1: 6 certificate(s)

Changes from the S3 file to your file:
  + ADDED      CN=Tailspin Partner Root CA G2, O=Tailspin Toys, C=GB
               root CA
               ECDSA P-256, ecdsa-with-SHA256, valid 2026-09-01 to 2041-09-01
               SHA-256 D5:8D:62:B0:…:01:B1:11:CE
               # Tailspin partner root CA G2 (RITM0012399)
  - REMOVED    CN=Fabrikam Root CA, O=Fabrikam Inc, C=GB
               ...
  ~ REPLACED   CN=Northwind Root CA, O=Northwind Traders, C=GB
               root CA; RSA 3072, sha256WithRSAEncryption, new key
               old: valid 2016-12-31 to 2026-12-31, SHA-256 98:20:6E:A9:…
               new: valid 2026-10-01 to 2036-12-31, SHA-256 29:40:F1:87:…
  = 4 unchanged: ...

Checks:
  WARNING  #4 (line 85) CN=Partner Issuing CA 2, O=Partner, C=GB: expires 2026-11-05 (in 14 days)

Summary: 1 added, 1 removed, 1 replaced, 4 unchanged (6 -> 6 certificates).

Upload truststore/prod/ca-bundle.pem to s3://ims-mtls-truststore-prod/mtls/ca-bundle.pem and update trust store ims-mtls-prod (1 warning(s) above)? Type yes to apply: yes

Saved the current S3 file to ~/truststore-backups/ims-mtls-prod-20261022-043007.pem
Uploaded truststore/prod/ca-bundle.pem to s3://…/mtls/ca-bundle.pem, version 3HL4…v2 (read back: identical)
Updating trust store ims-mtls-prod:
  04:30:28  pending
  04:30:35  active

Done: trust store ims-mtls-prod is active with 6 CA certificates, the same as your file.
```

**Errors that stop it before anything changes:**
```
  ERROR    line 119: comments contain "-", which CloudFront does not allow in comments. First one: # Tailspin partner root CA G2, added 2026-10-22
  ERROR    line 132: blank. CloudFront allows no blank lines in the bundle; delete them
  ERROR    #7 (line 133) CN=client01.tailspin.example, ...: not a CA certificate. It looks like a client's own certificate (issued by CN=Tailspin Partner Root CA G2, ...); the trust store needs the CA that issued it
```

**CloudFront rules worth knowing:**
- **25 certificates at most** per trust store, and that limit can't be raised.
- **No "-" in comments**, so dates like 2026-10-22 aren't allowed. Blank lines aren't allowed either.
- **Keys and signatures:** only RSA 2048/3072/4096 or ECDSA P-256/P-384 keys, with SHA-256/384/512 signatures.

The script also refuses private keys, CSRs and duplicate certificates. It also refuses certificates you're adding that are expired or are a client's own certificate rather than a CA. A certificate that's already in the trust store only gets a warning for these problems.

**What "yes" does:**
- **Backup:** saves the current S3 file under `~/truststore-backups`.
- **Upload:** puts your exact file at the same S3 key, keeping its KMS encryption, and reads it back to confirm.
- **Trust store update:** points the trust store at the new S3 version and keeps its OCSP setting as it is. It then waits until the trust store is active and checks that CloudFront counts the same number of certificates as your file.

**When it stops or warns:**
- **Stops** if you're logged in to the wrong account for that folder, or if someone changed the S3 file while you were reviewing.
- **Warns** if the PEM has uncommitted Git changes.
- **If CloudFront reports the update failed,** it prints the exact command to point the trust store back at the previous file.

The certificate reading matches the `cryptography` library on 122 real public root CAs. The rest is tested against simulated CloudFront and S3, not your accounts. Its handling of a failed update is a best guess, because AWS doesn't document how a failed update behaves. Start with `--diff-only` on the test account.

If you'd like, I can add a GitHub Actions check that runs `--validate-only` on every PR that changes a PEM file, so bad files are caught before merge.

Sources:
- [Trust stores and certificate management – Amazon CloudFront](https://docs.aws.amazon.com/dk_dk/AmazonCloudFront/latest/DeveloperGuide/trust-stores-certificate-management.html)
- [CloudFront quotas (mTLS and trust stores)](https://docs.aws.amazon.com/pt_pt/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html)
- [boto3 update_trust_store](https://docs.aws.amazon.com/botocore/latest/reference/services/cloudfront/client/update_trust_store.html)
- [boto3 get_trust_store](https://docs.aws.amazon.com/botocore/latest/reference/services/cloudfront/client/get_trust_store.html)
- [CloudFront certificate revocation (OCSP)](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/certificate-revocation.md)