import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as ecrAssets from "aws-cdk-lib/aws-ecr-assets";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import * as path from "path";

export interface QuickGoogleAdsConnectorStackProps extends cdk.StackProps {
  /**
   * Google Cloud OAuth 2.0 client ID. Required -- it is a security control (the
   * connector rejects tokens whose `aud` is not this). See DESIGN.md.
   */
  oauthClientId: string;

  /**
   * Developer token supplied inline. Visible in the CloudFormation template and
   * Lambda console, so prefer `developerTokenSecretArn` for anything shared.
   * Exactly one of the two must be set.
   */
  developerToken?: string;

  /**
   * ARN of a Secrets Manager secret holding the developer token. Read at init
   * and granted read on that one secret only, so it never enters the template.
   */
  developerTokenSecretArn?: string;

  /**
   * Manager (MCC) account customer ID, digits only. Needed to query client
   * accounts under a manager.
   */
  loginCustomerId?: string;

  /** API Gateway stage name. @default "prod" */
  stageName?: string;
}

export class QuickGoogleAdsConnectorStack extends cdk.Stack {
  /** Base URL to paste into the Amazon Quick MCP connector. */
  public readonly proxyUrl: string;
  public readonly lambdaFunction: lambda.DockerImageFunction;
  public readonly api: apigateway.RestApi;

  constructor(
    scope: Construct,
    id: string,
    props: QuickGoogleAdsConnectorStackProps
  ) {
    super(scope, id, props);

    cdk.Tags.of(this).add("workload", "quick-connector");

    if (!props.developerToken === !props.developerTokenSecretArn) {
      throw new Error(
        "Set exactly one of developerToken or developerTokenSecretArn."
      );
    }

    const stageName = props.stageName ?? "prod";

    const logGroup = new logs.LogGroup(this, "ConnectorFunctionLogGroup", {
      logGroupName: "/aws/lambda/quick-google-ads-connector",
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // REST (not HTTP API) and REGIONAL: the 29s integration timeout is raisable
    // on Regional REST APIs and WAF can front them. See DESIGN.md.
    this.api = new apigateway.RestApi(this, "ConnectorApi", {
      restApiName: "quick-google-ads-connector-api",
      description: "Amazon Quick <-> Google Ads MCP connector",
      endpointConfiguration: {
        types: [apigateway.EndpointType.REGIONAL],
      },
      deployOptions: {
        stageName,
        // Cheap insurance against a runaway client or a cost attack.
        throttlingRateLimit: 100,
        throttlingBurstLimit: 200,
      },
    });

    // Built by hand rather than from this.api.url to avoid a circular dependency
    // between the function's environment and the API that integrates with it.
    const proxyUrlValue = `https://${this.api.restApiId}.execute-api.${this.region}.amazonaws.com/${stageName}`;

    this.lambdaFunction = new lambda.DockerImageFunction(
      this,
      "ConnectorFunction",
      {
        functionName: "quick-google-ads-connector",
        // Pin platform and architecture explicitly so the image does not inherit
        // the build machine's arch (an arm64 build won't run on an x86_64
        // function). x86_64 matches the documented CloudShell build path.
        architecture: lambda.Architecture.X86_64,
        code: lambda.DockerImageCode.fromImageAsset(
          path.join(__dirname, "../app"),
          { platform: ecrAssets.Platform.LINUX_AMD64 }
        ),
        // Matches API Gateway's 29s integration cap (a longer timeout would only
        // bill for work the caller can no longer receive). See DESIGN.md.
        timeout: cdk.Duration.seconds(29),
        // Deliberate, and not a cold-start dial: raising it does not help (the
        // init is a single-threaded import). See DESIGN.md. Change if a future
        // upstream needs more memory.
        memorySize: 2048,
        logGroup,
        description:
          "Runs google-ads-mcp behind the OAuth compatibility layer Quick requires",
        environment: {
          PROXY_URL: proxyUrlValue,
          GOOGLE_OAUTH_CLIENT_ID: props.oauthClientId,
          ...(props.developerToken
            ? { GOOGLE_ADS_DEVELOPER_TOKEN: props.developerToken }
            : {}),
          ...(props.developerTokenSecretArn
            ? {
                GOOGLE_ADS_DEVELOPER_TOKEN_SECRET_ARN:
                  props.developerTokenSecretArn,
              }
            : {}),
          ...(props.loginCustomerId
            ? { GOOGLE_ADS_LOGIN_CUSTOMER_ID: props.loginCustomerId }
            : {}),
        },
      }
    );

    // Read access to exactly this one secret, nothing wider.
    if (props.developerTokenSecretArn) {
      secretsmanager.Secret.fromSecretCompleteArn(
        this,
        "DeveloperTokenSecret",
        props.developerTokenSecretArn
      ).grantRead(this.lambdaFunction);
    }

    const integration = new apigateway.LambdaIntegration(this.lambdaFunction, {
      proxy: true,
    });

    // The application does its own routing (MCP at "/", OAuth metadata under
    // /.well-known, /oauth/authorize), so everything is forwarded verbatim.
    this.api.root.addMethod("ANY", integration);
    this.api.root.addProxy({
      defaultIntegration: integration,
      anyMethod: true,
    });

    this.proxyUrl = proxyUrlValue;

    new cdk.CfnOutput(this, "AmazonQuickSettings", {
      value: [
        `MCP server endpoint: ${this.proxyUrl}`,
        `Authorization URL:   ${this.proxyUrl}/oauth/authorize`,
        `Token URL:           https://oauth2.googleapis.com/token`,
        `Client ID:           <your Google Cloud OAuth 2.0 client ID>`,
        `Client Secret:       <your Google Cloud OAuth 2.0 client secret>`,
        `Public OAuth client: No`,
      ].join("\n"),
      description: "Paste these into the Amazon Quick MCP connector",
    });

    new cdk.CfnOutput(this, "GoogleRedirectUriToAllowlist", {
      value: `https://${this.region}.quicksight.aws.amazon.com/sn/oauthcallback`,
      description:
        "Add this to Authorized redirect URIs on your Google Cloud OAuth client",
    });

    new cdk.CfnOutput(this, "HealthCheckUrl", {
      value: `${this.proxyUrl}/health`,
      description: "Curl this first -- it warms the function and proves routing",
    });
  }
}
