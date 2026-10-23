🧪 1 · Validate trust store summary
🧪 Validation — PASS
Environment: prd
Proposed bundle: cloudformation/deamon/ims-app-identity-main/ims-app-identity-main/truststore/prd/truststore.pem

Validating: /home/runner/work/AWS-Landing-Zone/AWS-Landing-Zone/cloudformation/deamon/ims-app-identity-main/ims-app-identity-main/scripts/../truststore/prd/truststore.pem
Certificate count: 20
---
File format: OK (LF line endings, no blank lines, no trailing whitespace)
---
INDEX   STATUS          EXPIRES      DAYS   TYPE        CN                                                 OU                   O                                        C   
[1]     VALID           2036-05-06   3483   Self-signed tescoinsurance.com Root CA 01 (Production)         Prd                  Tesco Personal Finance Limited           GB  �[0m
[2]     VALID           2031-05-07   1657   CA-signed   tescoinsurance.com Issuing CA 01 (Production)      Prd                  Tesco Personal Finance Limited           GB  �[0m
[3]     VALID           2046-03-21   7089   Self-signed Sectigo Public Server Authentication Root R46      -                    Sectigo Limited                          GB  �[0m
[4]     VALID           2027-12-10   413    CA-signed   Entrust EV TLS Issuing RSA CA 2                    -                    Entrust Limited                          CA  �[0m
[5]     VALID           2043-08-03   6128   CA-signed   Redde Integration Services CA                      Redde Integration Se Redde plc                                GB  �[0m
[6]     VALID           2068-07-05   15231  Self-signed Redde plc Root Certificate Authority               Group IT             Redde plc                                GB  �[0m
[7]     VALID           2034-12-10   2969   Self-signed GlobalSign                                         GlobalSign Root CA - GlobalSign                               -   �[0m
[8]     VALID           2032-02-24   1950   Self-signed Aviva Offline Internal Root CA                     -                    Aviva PLC                                GB  �[0m
[9]     VALID           2032-02-20   1946   CA-signed   Aviva UK Internal Issuing CA                       -                    Aviva PLC                                GB  �[0m
[10]    VALID           2029-03-18   877    Self-signed GlobalSign                                         GlobalSign Root CA - GlobalSign                               -   �[0m
[11]    VALID           2028-11-21   759    CA-signed   GlobalSign RSA OV SSL CA 2018                      -                    GlobalSign nv-sa                         BE  �[0m
[12]    VALID           2046-03-21   7089   Self-signed Sectigo Public Server Authentication Root R46      -                    Sectigo Limited                          GB  �[0m
[13]    VALID           2027-12-10   413    CA-signed   Entrust EV TLS Issuing RSA CA 2                    -                    Entrust Limited                          CA  �[0m
[14]    VALID           2038-01-15   4102   Self-signed DigiCert Global Root G2                            www.digicert.com     DigiCert Inc                             US  �[0m
[15]    VALID           2027-11-02   375    CA-signed   Thawte TLS RSA CA G1                               www.digicert.com     DigiCert Inc                             US  �[0m
[16]    VALID           2045-10-07   6923   CA-signed   ca.prod.tims.proofidcloud.com                      Cloud Services - Cus ProofID Ltd                              GB  �[0m
[17]    VALID           2037-09-12   3977   CA-signed   customer.ca.hosted.proofidcloud.com                Cloud Services - Cus ProofID Ltd                              GB  �[0m
[18]    VALID           2037-09-12   3977   Self-signed ca.hosted.proofidcloud.com                         Cloud Services       ProofID Ltd                              GB  �[0m
[19]    VALID           2027-07-30   280    Self-signed TescoUT1 IMS Hyland API Client Certificate         CPE                  Hyland Software Inc.                     US  �[0m
[20]    VALID           2039-10-17   4742   Self-signed PureCloud Data Actions Root CA (prod-euw1 eu-west- PureCloud            Genesys                                  US  �[0m
---
Summary: 20 certificate(s) checked
  Expired          : 0
  Not yet valid    : 0
  Expiring <= 30d  : 0
  Not a CA cert    : 0
  Self-signed      : 11
  Duplicate cert(s): 2
  Unparsable       : 0

�[33mNOTE: 11 self-signed certificate(s) found (subject == issuer).�[0m
This is expected for a root CA anchoring the chain, but a self-signed
intermediate/leaf entry is unusual and can be rejected or misapplied by a
CloudFront trust store, which expects a proper CA chain for client-cert (mTLS)
validation. Confirm any self-signed entry above is genuinely a trusted root.

�[33mNOTE: 2 exact duplicate certificate(s) found (same SHA-256 fingerprint at more than one index):�[0m
  7B:B6:47:A6:2A:EE:AC:88:BF:25:7A:A5:22:D0:1F:FE:A3:95:E0:AB:45:C7:3F:93:F6:56:54:EC:38:F2:5A:06 -> indices: 12 3
  5A:A0:5D:C4:23:38:E1:6D:AC:33:0A:80:57:47:F7:ED:F2:96:DE:46:54:6B:DC:B6:07:18:4E:98:F1:63:7C:1A -> indices: 13 4

Done
🔐 Target account
Environment: prd
Role alias: NETWORK
Account ID: 584477831165
Trust store ARN: arn:aws:cloudfront::584477831165:trust-store/ts_3DRZ7XCOPMm7crTtaA2p99ngcr5
Job summary generated at run-time
🔍 2 · Compare with S3 summary
🔍 S3 comparison
Environment: prd
Current S3 object: s3://inf-prd-584477831165-cloudfront-truststore/truststore.pem
Proposed CA certificates: 18
Current CA certificates: 19
Added certificates: 2
Removed certificates: 3
Result: Changes detected
\nAdded SHA-256 fingerprints
56650C7252C3A6869F249581B6EDC3B38EAF968A2E1466485BC3D8376311C993
A1D9A0587E15C0E0E2D732C1EDBBB201EA857A342BE42AD951F66FFE5E17A653
\nRemoved SHA-256 fingerprints
5536553D1CEC3F1234128495C12D64F15DD388DBBBC13ED31AB757D222F876DE
674D91CB7B97A3C19F9B3ABC832710B17C21E7416FAD74CB3F715DDBAF050DC5
A4C1515606CD7E010F5158CC88CF8B03C35CD4D8080D283F56CCEABD924CDAB5
Job summary generated at run-time
⏸️ 3 · Apply skipped (review only) summary
⏸️ Apply skipped
Validation and comparison completed. No changes were made to S3 or CloudFront.
Re-run the workflow with Apply = yes to apply the proposed bundle.

Job summary generated at run-time
Annotations
2 warnings and 3 notices
🧪 1 · Validate trust store
Node.js 20 is deprecated. The following actions target Node.js 20 but are being forced to run on Node.js 24: actions/checkout@v4, aws-actions/configure-aws-credentials@v4. For more information see: https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/
🔍 2 · Compare with S3
Node.js 20 is deprecated. The following actions target Node.js 20 but are being forced to run on Node.js 24: actions/checkout@v4, aws-actions/configure-aws-credentials@v4. For more information see: https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/
🧪 1 · Validate trust store
"The ubuntu-latest label will migrate to Ubuntu 26 beginning October 19, 2026. For more information, see https://github.com/actions/runner-images/issues/14748"
🔍 2 · Compare with S3
"The ubuntu-latest label will migrate to Ubuntu 26 beginning October 19, 2026. For more information, see https://github.com/actions/runner-images/issues/14748"
⏸️ 3 · Apply skipped (review only)
"The ubuntu-latest label will migrate to Ubuntu 26 beginning October 19, 2026. For more information, see https://github.com/actions/runner-images/issues/14748"
