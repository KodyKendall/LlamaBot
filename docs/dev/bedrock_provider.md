# AWS Bedrock provider (LP-29, 0.7.20)

`claude-haiku-5.5-bedrock` runs Claude Haiku 5.5 from the box's own AWS account,
so prompts never leave it. Code: `app/agents/leonardo/bedrock.py`.

## What ships

| Model | Status |
|---|---|
| Claude Haiku 5.5 → `claude-haiku-5.5-bedrock` | Registered. Images yes, PDF off (unverified on Bedrock), video no. |
| GPT-6 Luna → `gpt-6-luna-bedrock` | **Not registered.** OpenAI's models on Bedrock are the open-weight gpt-oss family only. No substitute was added. |

## Turning it on for a box

1. **Run on AWS with an instance role.** EC2 instance profile or ECS task role.
   Static keys are refused: if `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
   `AWS_SESSION_TOKEN` are in the env, Bedrock reports unavailable and logs why.
2. **EC2 + Docker: set the IMDSv2 hop limit to 2.** LlamaBot runs in a container
   on the Docker bridge, one hop further than the host. With the default limit of 1,
   the token request never comes back and the model shows as unavailable.
   `aws ec2 modify-instance-metadata-options --instance-id <id> --http-tokens required --http-put-response-hop-limit 2`
3. **Enable model access** in the Bedrock console (us-west-2) for Anthropic Claude
   Haiku 5.5. Anthropic models need the one-time use-case form.
4. **Verify the model id.** It could not be checked from the dev box.
   `aws bedrock list-foundation-models --region us-west-2 --by-provider anthropic --query "modelSummaries[?contains(modelId,'haiku')].[modelId,inferenceTypesSupported]"`
   - If the on-demand id differs from the default (`anthropic.claude-haiku-5-5-v1:0`),
     set `BEDROCK_HAIKU_5_5_MODEL_ID`.
   - If only `INFERENCE_PROFILE` is listed, set it to the profile id (`us.anthropic.…`).
5. **Set `BEDROCK_REGION=us-west-2`** in the box `.env`. This is the opt-in; without it
   the model is unavailable and AWS is never contacted.
6. **Enable the model in the box's model policy.** With no pushed allow-list a box
   offers only its two default models, so the mothership must list
   `claude-haiku-5.5-bedrock` in `enabled_models` (same as any non-default model).
7. **ZDR boxes:** the build marks this model ZDR-compliant, but the mothership must
   still list `claude-haiku-5.5-bedrock` in `zdr.allowed_models`.

## IAM policy for the instance role

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": [
      "arn:aws:bedrock:us-west-2::foundation-model/<model id from step 4>"
    ]
  }]
}
```

If you use an inference profile, also allow
`arn:aws:bedrock:us-west-2:<account>:inference-profile/<profile id>` and the
foundation-model ARN in every region the profile routes to.

## Not verified yet (needs a real AWS box)

- The default model id.
- Adaptive thinking (`additional_model_request_fields.thinking`) accepted through Converse.
- PDF uploads (kept off).
