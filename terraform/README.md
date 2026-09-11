# Infrastructure

Terraform for running `booking_service` and `voice_gateway` on real AWS
infrastructure, instead of the local `docker compose` + ngrok tunnel
setup in the project's [`docker-compose.yml`](../docker-compose.yml).

## What this provisions

- **VPC** with 2 public subnets (the EC2 host) and 2 private subnets
  (RDS only) — no NAT Gateway, since nothing in this VPC needs outbound
  internet access on its own.
- **One EC2 instance**, running the ECS agent (EC2 launch type, not
  Fargate — cheaper for a single always-on demo) plus nginx and certbot
  for TLS termination. No ALB, no SSH — access is via
  `aws ssm start-session`.
- **Two ECS services**, colocated on that one instance via bridge
  networking with static host ports, mirroring `docker-compose.yml`'s own
  8000/8001 split:
  - `booking_service` — host port 8000, **not** internet-facing.
  - `voice_gateway` — host port 8001, reverse-proxied by nginx on 80/443.
- **RDS PostgreSQL**, in the private subnets, reachable only from the EC2
  host's security group.
- **Two ECR repositories**, one per service.
- **A dedicated IAM user** for Bedrock (`amazon.nova-2-sonic-v1:0`) —
  `bedrock_stream.py` authenticates with its static keys via
  `EnvironmentCredentialsResolver()`, not an ECS task role.
- **SSM Parameter Store** (`SecureString`) for every secret: the AWS key
  pair, the Twilio auth token, and the generated database password.
  Nothing sensitive is written into a task definition or a `.tf` file.
- **Route53** A record + **CloudWatch** log groups (one per service).

## Deploying

1. **Copy the tfvars template and fill it in:**

   ```bash
   cp terraform.tfvars.example terraform.tfvars
   ```

2. **Provision the infrastructure:**

   ```bash
   terraform init
   terraform apply
   ```

   The ECS services will come up in a failing state at this point — there
   are no images in ECR yet. That's expected.

3. **Build and push both images:**

   ```bash
   ../scripts/deploy.sh
   ```

   This re-runs `terraform output` internally, so it needs to be run from
   a shell that can reach the state created in step 2.

4. **One-time TLS certificate**, once the Route53 record has actually
   propagated to the EC2 instance's Elastic IP:

   ```bash
   aws ssm start-session --target "$(terraform output -raw ec2_instance_id)"
   sudo certbot --nginx -d <your domain> -m <your admin email> --agree-tos --redirect
   ```

5. **Point Twilio's voice webhook** at
   `terraform output -raw public_url` + `/twilio/voice`.

## Notes

- `booking_service`'s `/tools/*` routes have no auth of their own — they
  rely entirely on not being reachable from outside the EC2 host (see
  `ec2.tf`'s security group, and the README's own "not built yet" list).
  This infrastructure preserves that gap; it doesn't fix it.
- Destroying this (`terraform destroy`) is safe to do and re-apply for a
  demo — `deletion_protection = false` and `skip_final_snapshot = true`
  on the RDS instance are deliberate for that, and would be the first
  things to flip for anything closer to production.
