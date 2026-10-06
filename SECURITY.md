# Security

Found a vulnerability? Please **report it privately** — open a [security advisory](https://github.com/autonomous-ai/Physical-AI-Operating-System/security/advisories/new)
or email the maintainer instead of filing a public issue, and give us a chance to ship a fix
first. Thank you. 🙏

One thing to get right in production: the on-device HAL authenticates with a
`device_auth_token` that is **separate from your LLM provider key** — keep them distinct so
leaking a model-billing key can't hand someone control of the hardware.
