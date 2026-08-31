#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { QuickGoogleAdsConnectorStack } from "../lib/quick-google-ads-connector-stack";

const app = new cdk.App();

const developerToken =
  app.node.tryGetContext("developerToken") ??
  process.env.GOOGLE_ADS_DEVELOPER_TOKEN;

const developerTokenSecretArn =
  app.node.tryGetContext("developerTokenSecretArn") ??
  process.env.GOOGLE_ADS_DEVELOPER_TOKEN_SECRET_ARN;

const oauthClientId =
  app.node.tryGetContext("oauthClientId") ?? process.env.GOOGLE_OAUTH_CLIENT_ID;

if (!oauthClientId) {
  throw new Error(
    [
      "Missing Google OAuth client ID.",
      "",
      "  npx cdk deploy -c oauthClientId=YOUR_CLIENT_ID.apps.googleusercontent.com",
      "",
      "The same client ID you enter in Amazon Quick. It's required as an access",
      "control -- it pins which OAuth client's tokens this endpoint accepts",
      "(see DESIGN.md).",
    ].join("\n")
  );
}

if (!developerToken && !developerTokenSecretArn) {
  throw new Error(
    [
      "Missing Google Ads developer token.",
      "",
      "Recommended -- keep it in Secrets Manager:",
      "  aws secretsmanager create-secret --name google-ads-developer-token \\",
      "      --secret-string YOUR_TOKEN",
      "  npx cdk deploy -c developerTokenSecretArn=arn:aws:secretsmanager:...",
      "",
      "Or inline, which leaves the value readable in the CloudFormation",
      "template and the Lambda console:",
      "  npx cdk deploy -c developerToken=YOUR_TOKEN",
      "",
      "The token needs at least Explorer access to query production accounts.",
    ].join("\n")
  );
}

if (developerToken && developerTokenSecretArn) {
  throw new Error(
    "Pass either developerToken or developerTokenSecretArn, not both."
  );
}

const loginCustomerId =
  app.node.tryGetContext("loginCustomerId") ??
  process.env.GOOGLE_ADS_LOGIN_CUSTOMER_ID;

if (!loginCustomerId) {
  throw new Error(
    [
      "Missing loginCustomerId (manager account / MCC ID).",
      "",
      "  npx cdk deploy -c loginCustomerId=1234567890   # digits only, no dashes",
      "",
      "The developer token comes from a manager account, so queries run through",
      "it. Without this, deploy would succeed but every query against a client",
      "account fails with USER_PERMISSION_DENIED. It is the ID of the 'Manager'",
      "account in your Google Ads account switcher.",
    ].join("\n")
  );
}

new QuickGoogleAdsConnectorStack(app, "QuickGoogleAdsConnectorStack", {
  oauthClientId,
  developerToken,
  developerTokenSecretArn,
  loginCustomerId,
  stageName: app.node.tryGetContext("stageName"),
});
