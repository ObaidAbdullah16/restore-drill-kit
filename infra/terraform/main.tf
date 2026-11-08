# "Production" side of the restore drill demo: a small RDS PostgreSQL instance with
# automated backups, an S3 bucket for backup manifests, and an empty security
# group that the drill opens for one IP at a time.
terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  backend "s3" {}
}

variable "aws_region" {
  type    = string
  default = "ap-southeast-2"
}

variable "name" {
  type    = string
  default = "restore-drill"
}

variable "instance_class" {
  type    = string
  default = "db.t4g.micro"
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project   = "restore-drill-kit"
      ManagedBy = "terraform"
      AutoStop  = "true"
      DataClass = "synthetic"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

resource "aws_vpc" "this" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = "${var.name}-vpc" }
}

# Lock down the VPC's default security group (nothing should use it).
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.this.id
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(aws_vpc.this.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = { Name = "${var.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# No ingress rules on purpose: `restoredrill` adds <your-ip>/32 on port 5432 only
# while it seeds or verifies, and removes it straight after.
resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "Postgres access is opened temporarily by the drill tool"
  vpc_id      = aws_vpc.this.id
  # No egress either: the database never needs to call out.
}

# Restored sandbox copies get their OWN empty security group. The drill opens this one for your IP
# while verifying, so the "production" database is never reachable during a drill.
resource "aws_security_group" "sandbox" {
  #checkov:skip=CKV2_AWS_5:attached at restore time by restoredrill, not by Terraform
  name        = "${var.name}-sandbox"
  description = "Restored sandbox copies; opened temporarily by the drill tool"
  vpc_id      = aws_vpc.this.id
}

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = aws_subnet.public[*].id
}

resource "random_password" "db" {
  length  = 24
  special = false
}

resource "aws_ssm_parameter" "db_password" {
  name  = "/${var.name}/db-password"
  type  = "SecureString"
  value = random_password.db.result
}

resource "aws_db_instance" "source" {
  identifier                 = "${var.name}-prod"
  engine                     = "postgres"
  engine_version             = "16"
  instance_class             = var.instance_class
  allocated_storage          = 20
  storage_type               = "gp3"
  storage_encrypted          = true
  db_name                    = "shop"
  username                   = "drill"
  password                   = random_password.db.result
  db_subnet_group_name       = aws_db_subnet_group.this.name
  vpc_security_group_ids     = [aws_security_group.db.id]
  publicly_accessible        = true # demo only: reachable from your IP while the drill runs; use a bastion/SSM for clients
  backup_retention_period    = 1    # enables automated backups + point-in-time restore
  backup_window              = "20:00-20:30"
  copy_tags_to_snapshot      = true
  delete_automated_backups   = true
  deletion_protection        = false
  skip_final_snapshot        = true
  apply_immediately          = true
  auto_minor_version_upgrade = true

  iam_database_authentication_enabled = true
}

resource "aws_s3_bucket" "manifests" {
  bucket        = "${var.name}-manifests-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "manifests" {
  bucket = aws_s3_bucket.manifests.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "manifests" {
  bucket = aws_s3_bucket.manifests.id
  rule {
    id     = "expire-old-manifests"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_public_access_block" "manifests" {
  bucket                  = aws_s3_bucket.manifests.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "manifests" {
  bucket = aws_s3_bucket.manifests.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

output "source_id" {
  value = aws_db_instance.source.identifier
}

output "endpoint" {
  value = aws_db_instance.source.address
}

output "port" {
  value = aws_db_instance.source.port
}

output "db_name" {
  value = aws_db_instance.source.db_name
}

output "username" {
  value = aws_db_instance.source.username
}

output "sg_id" {
  value = aws_security_group.db.id
}

output "sandbox_sg_id" {
  value = aws_security_group.sandbox.id
}

output "subnet_group" {
  value = aws_db_subnet_group.this.name
}

output "password_ssm_name" {
  value = aws_ssm_parameter.db_password.name
}

output "manifest_uri" {
  value = "s3://${aws_s3_bucket.manifests.bucket}/manifests/latest.json"
}
