Subject: Central WAF management – IaC gap review: deck and agenda

Hi all,

Attached is a deck on the recent WAF incident and what it revealed about how we manage AWS WAF as code, including whether Terraform would help. I'd like 60 minutes to take you through it and agree four decisions.

When: [Day, DD Mon] at [time] – Teams invite to follow

In brief
• We run our AWS estate as code. IP sets and rule groups go through pull requests and the pipeline, and CloudFormation detects drift on them.
• The recent incident broke that promise. AWS changed the default version of the Bot Control managed rule group and, as we don't pin a version, our web ACLs moved to it automatically. Some legitimate traffic was labelled differently, one of our custom rules blocked it, and payments failed, with nothing changed in Git.
• It exposed two blind spots. The first is changes AWS makes for us; CHG0035667 (planned for 26 Nov) closes it by pinning the version and alerting us to AWS version changes. The second is changes to the Firewall Manager security policy made outside our code, which CloudFormation can't detect; AWS Support confirmed this (case 179118346100302).
• Terraform would detect those policy changes but wouldn't have caught this incident, and converting all our CloudFormation isn't simple. We propose a pilot for the security policy only.

Agenda
1. Background: how WAF protects our sites and how we run our estate as code (slides 3–5)
2. The incident (slides 6–7)
3. Our setup: one policy, many accounts (slides 8–11)
4. The challenge: two blind spots (slides 12–16)
5. Could Terraform help? (slides 17–22)
6. Way forward and decisions (slides 23–27)

Decisions needed
1. Agree that the security policy is managed in the console for now, under change control
2. Approve a daily check of the live policy, with an alert on any difference
3. Decide whether to pilot Terraform for the security policy, starting with one non-prod account
4. Choose sites for a local web ACL pilot

If you only have a few minutes beforehand, slides 7, 13, 18 and 27 carry the key points.

Regards,
Kunal Shrivastava
Platform Engineering
