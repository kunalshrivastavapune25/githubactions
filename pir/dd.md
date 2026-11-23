
I've drafted the justification email and a short Teams reply to Abhishek. The email covers all six changes, so remove any that aren't on Abhishek's list, and attach the six PIR files before sending. It says plainly that the timelines were reconstructed from the ServiceNow records, so nobody can later say that was hidden.

One problem with the PIRs themselves: each one currently lists the original change owner in its sign-off (Nikhil Chaudhari, Chennu Raghu Kishore or Nitin Sharma). Since they've left, they can't sign, which would block closure today. Want me to put you in as Change Owner on all six and regenerate them?


Hi Abhishek, noted. The engineers who implemented these changes have since left the team, and during hypercare they were tied up with other priority work, which is why the PIRs were missed. I've taken ownership: PIRs for CHG0031200, CHG0031562, CHG0031869, CHG0031941, CHG0031943 and CHG0032007 are ready, and I'll send them with the justification mail shortly so we can close them today. Siddhi, please share the remaining ones and I'll pick those up too.

Subject: Long-ageing AWS PIR closures – reason for delay and PIRs for closure

Hi Abhishek, Siddhi,

Following up on the long-ageing AWS PIR closures. I have taken ownership of these, and the PIRs are attached so they can be closed today as per Change Mandates.

Changes covered:
- CHG0031200 – PPE IBMi DNS A records (Feb 2026)
- CHG0031562 – AWS Network Firewall rules for DevOps CI/CD pipelines (Mar 2026)
- CHG0031869 – AWS and VMX firewall rules for AVD to SAS (Mar 2026)
- CHG0031941 – FSx Windows log forwarding to Core Log Archive and Sentinel (Apr 2026)
- CHG0031943 – Site24x7 poller and VMX security group rules (Apr 2026)
- CHG0032007 – FSx log forwarding follow-up to CHG0031941 (Apr 2026)

Why these took so long:
- These changes were implemented during the hypercare window, when the engineers who owned them were also committed to other priority project work (for example, the Fabric Data Gateway setup noted on CHG0032007). Post-implementation tasks, closure updates and PIRs were not completed at the time.
- Those engineers have since left the team. When the PIR requests came in on 26 August and the chasers followed, these changes had no named owner in the team, so they were not picked up. That is why the chasers went unanswered, and I apologise for the follow-ups this has needed.
- As the original implementers are no longer available, the PIRs have been prepared from the ServiceNow records (work notes, close notes, change tasks and approvals). Where exact times were not recorded, the timelines are reconstructed from the record history.

To stop this happening again:
- Before anyone leaves or moves off the team, their open changes, change tasks and PIRs will be reassigned to a named owner.
- PIRs and closure tasks will be completed within the Change Mandate timelines, and I will track Change Management chasers on AWS changes until they are closed.

Please let me know if you need anything else to close these today. If there are more changes on the list, please share them and I will pick them up.

Regards,
Kunal Shrivastava
