There are theree thins to manage in WAF 

 

We have Waf centrally managed in Security infra account its also call fms delegated admin account 

Here we configure three trhins 

IPSets 

RuleGroups  

Scurity Policy 

 

Simple use case is when requirement is i want list of 5 ips to deny access from some clouffront website 

For this use case we are going to cere 1 ipset of 5 ips in ipset at central account 

Then a rule grop where we will mention deny rule 

Then a secutity policy there we will associate the rule group  from cludfront and account where which policy of webacl is applicable 

 Challange plateform team faces here 

Ipset is proper yml as shown below, so use case ot bau req for new ip addition to a ip set is possible from out pipeline and tac practice 

Rulwgroup is also proper yml as shown below, so use case ot bau req for any ip allow or denty  is possible from out pipeline and tac practice 

But the security policy the policy format is json and as per below mail from aws its not iac managed 

security infra case id 179118346100302 -  Case Details | AWS Support Console 

  

Hello Kunal,Thank you for contacting AWS Support. I'm Raghu from the CloudFormation team, and it was a pleasure working with you on this case. Please allow me to share a brief summary of our session.You reached out to us regarding a drift detection concern with your CloudFormation stack "ims-security-infra-fmspol-global-fmswafpol", which manages  AWS Firewall Manager (FMS) WAFv2 policies. You observed that two manual changes were made directly in the AWS console to the policy but CloudFormation drift detection continued to report the stack as IN_SYNC.We connected over the call, where I checked the stack "ims-security-infra-fmspol-global-fmswafpol" using our internal tools and found, it is using the resource type AWS::FMS::Policy. Upon reviewing the resource type support documentation [1], we can confirm that AWS::FMS::Policy does not support drift detection.Because drift detection is not yet supported for AWS::FMS::Policy, the resource will not be reported as drifted when you run drift detection, even when the underlying WAF rules are changed manually in the console. Given the above limitation, the custom approach you have built is currently the right option to detect this kind of drift.Next, we discussed on how drift-aware change sets in your pipeline would behave for this scenario. As noted in the documentation [2], drift-aware change sets support a comparison of the desired state against the actual state for hundreds of resource types. For unsupported resource types, drift-aware change sets fall back to comparing the previous deployment state with the desired state.Since AWS::FMS::Policy is not among the supported resource types, drift-aware change sets will not compare the actual live resource state for your FMS policies. They will only create a change set based on the difference between the previous template state and the desired template state, which means a manual console change to ManagedServiceData will not surface through this mechanism either.As mentioned, I have created a feature request with our internal team to add drift support for AWS::FMS::Policy. But I am afraid, I am not able to provide an ETA for when this will be added, because internal teams do not share timelines with support engineers.We concluded the call, as there were no further queries. I will be marking this case as resolved, as agreed.Should you have any further queries or concerns, please feel free to reach out to us with a different case, and we will be happy to assist you.Thank you and have an AWSome day ahead :)References:[1]. Resource type support - AWS CloudFormation[2]. Using drift-aware change sets - AWS CloudFormationWe value your feedback. Please share your experience by rating this and other correspondences in the AWS Support Center. You can rate a correspondence by selecting the stars in the top right corner of the correspondence.Best regards,Raghu L.Amazon Web Services 

  

  
image 

  

   

 

 
