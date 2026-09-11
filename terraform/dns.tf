# DNS: points the domain at the EC2 instance's stable Elastic IP.
#
# The hosted zone itself is created manually in the Route53 Console (not
# by Terraform) - if the domain was bought through a third-party
# registrar rather than Route53, there's no aws_route53domains
# registration for Terraform to manage. This just looks up the zone that
# already exists by name.
data "aws_route53_zone" "app" {
  name = var.domain_name
}

resource "aws_route53_record" "app" {
  zone_id = data.aws_route53_zone.app.zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}
