import * as path from 'node:path';
import {
  CfnOutput, Duration, RemovalPolicy, Stack, StackProps,
} from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';

const APP_PORT = 8000;
const SECRET_NAME = 'space-watch/bedrock-bearer';

export class SpaceWatchStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);

    // CloudFront 커스텀 헤더는 synth 시점에 리터럴 값을 요구하므로 Secrets Manager
    // 참조가 해석되지 않는다. 그래서 이 값만은 IaC 입력으로 들어올 수밖에 없다.
    // 버전관리에는 두지 않는다 — 이 값이 ALB DNS 와 함께 공개되면 X-Origin-Verify
    // 방어층이 무력화된다(누구나 자기 CloudFront 배포로 통과할 수 있다).
    // 값은 gitignore 된 infra/.origin-verify 에 있고 `npm run deploy` 가 넣어준다.
    const originVerifyValue =
      process.env.SW_ORIGIN_VERIFY || (this.node.tryGetContext('originVerifyValue') as string);
    if (!originVerifyValue) {
      throw new Error(
        'SW_ORIGIN_VERIFY 환경변수가 필요합니다. `cd infra && npm run deploy` 를 쓰거나\n' +
        '  export SW_ORIGIN_VERIFY=$(cat infra/.origin-verify)\n' +
        '값이 없으면 새로 만드세요: python3 -c "import secrets;print(secrets.token_urlsafe(32))" > infra/.origin-verify'
      );
    }
    const prefixListId = this.node.tryGetContext('cloudfrontOriginFacingPrefixListId') as string;
    if (!prefixListId) {
      throw new Error('cdk.json context 에 cloudfrontOriginFacingPrefixListId 가 없습니다.');
    }

    // -----------------------------------------------------------------------
    // 네트워크 — 퍼블릭 서브넷만, NAT Gateway 없음
    //
    // Fargate 태스크는 LL2/CelesTrak/Bedrock 으로 아웃바운드가 필요하다. NAT
    // Gateway(시간당 과금)를 두는 대신 퍼블릭 서브넷 + assignPublicIp 로 해결하고,
    // 인바운드는 SG 로 차단해 외부에서 태스크에 직접 닿지 못하게 한다.
    // -----------------------------------------------------------------------
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
      ],
    });

    // -----------------------------------------------------------------------
    // 비밀 — 껍데기만 만들고 값은 사용자가 넣는다.
    // 이미지에도, 이 코드에도 값이 없다.
    // -----------------------------------------------------------------------
    const bearerSecret = new secretsmanager.Secret(this, 'BedrockBearer', {
      secretName: SECRET_NAME,
      // 배포 후 put-secret-value 로 실제 값을 넣는다.
      description: 'Bedrock API key (Bearer token). Populate with put-secret-value after deploy.',
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // -----------------------------------------------------------------------
    // 컨테이너 — 빌드 호스트가 aarch64 이므로 ARM64 로 맞춘다.
    // 아키텍처가 어긋나면 태스크가 exec format error 로 즉사한다.
    // -----------------------------------------------------------------------
    const image = new ecrAssets.DockerImageAsset(this, 'Image', {
      directory: path.join(__dirname, '..', '..'),
      platform: ecrAssets.Platform.LINUX_ARM64,
      exclude: ['.venv', 'infra', 'docs', '.remember', 'cdk.out'],
    });

    const cluster = new ecs.Cluster(this, 'Cluster', { vpc, containerInsightsV2: ecs.ContainerInsights.DISABLED });

    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: 256,
      memoryLimitMiB: 512,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    taskDef.addContainer('app', {
      image: ecs.ContainerImage.fromDockerImageAsset(image),
      portMappings: [{ containerPort: APP_PORT }],
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'space-watch',
        logRetention: logs.RetentionDays.ONE_WEEK,
      }),
      // 기동 시 주입. 태스크 롤에 bedrock:* 는 부여하지 않는다 —
      // Bearer 인증은 SigV4 IAM 경로를 타지 않는다.
      secrets: {
        AWS_BEARER_TOKEN_BEDROCK: ecs.Secret.fromSecretsManager(bearerSecret),
      },
      environment: {
        SW_POLL_INTERVAL: '1200',   // LL2 익명 한도 15 req/hr, 사이클당 4요청
        SW_CACHE_MAX_AGE: '3600',
      },
    });

    // -----------------------------------------------------------------------
    // 보안 그룹 — ALB 는 CloudFront 관리형 prefix list 에서만, 태스크는 ALB 에서만
    // -----------------------------------------------------------------------
    const albSg = new ec2.SecurityGroup(this, 'AlbSg', {
      vpc, // ALB: CloudFront origin-facing prefix list 에서만 인바운드
      description: 'ALB - inbound only from CloudFront origin-facing prefix list',
      allowAllOutbound: true,
    });
    albSg.addIngressRule(
      ec2.Peer.prefixList(prefixListId),
      ec2.Port.tcp(80),
      'CloudFront edge only - the public internet cannot reach the ALB directly',
    );

    const serviceSg = new ec2.SecurityGroup(this, 'ServiceSg', {
      vpc, // Fargate 태스크: ALB 에서만 인바운드
      description: 'Fargate tasks - inbound only from the ALB security group',
      allowAllOutbound: true,
    });
    serviceSg.addIngressRule(albSg, ec2.Port.tcp(APP_PORT), 'From ALB only');

    const service = new ecs.FargateService(this, 'Service', {
      cluster,
      taskDefinition: taskDef,
      // 스토어가 프로세스 메모리이므로 1로 고정한다. 2개면 두 화면이 서로 다른
      // 스냅샷을 보게 되고 ALB 라운드로빈 때문에 값이 깜빡인다.
      desiredCount: 1,
      assignPublicIp: true,   // NAT 없이 아웃바운드
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [serviceSg],
      minHealthyPercent: 0,   // 태스크 1개짜리 서비스에서 롤링 배포가 멈추지 않게
      circuitBreaker: { rollback: true },
    });

    // -----------------------------------------------------------------------
    // ALB — 기본 403, X-Origin-Verify 헤더가 맞을 때만 포워드
    // 프리픽스 리스트(누가) + 헤더(무엇을 아는가) 2중 방어
    // -----------------------------------------------------------------------
    const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc, internetFacing: true, securityGroup: albSg,
    });

    const targetGroup = new elbv2.ApplicationTargetGroup(this, 'Tg', {
      vpc,
      port: APP_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      targets: [service],
      deregistrationDelay: Duration.seconds(10),
      healthCheck: {
        path: '/healthz',   // 수집 실패 중에도 200 을 내도록 구현돼 있다
        interval: Duration.seconds(30),
        timeout: Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
    });

    const listener = alb.addListener('Http', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      // open: false 가 필수다. 기본값 true 면 CDK 가 리스너 포트에 0.0.0.0/0
      // 인바운드 규칙을 자동으로 추가하는데, 보안 그룹 규칙은 합집합이므로 위에서
      // 붙인 prefix list 규칙과 OR 로 합쳐져 넓은 쪽이 이긴다. 결과적으로
      // prefix list 제한이 있다고 믿으면서 ALB 는 인터넷에 열려 있게 된다.
      open: false,
      defaultAction: elbv2.ListenerAction.fixedResponse(403, {
        contentType: 'text/plain',
        messageBody: 'Direct origin access is not allowed. Use the CloudFront endpoint.',
      }),
    });

    listener.addAction('ViaCloudFront', {
      priority: 10,
      conditions: [elbv2.ListenerCondition.httpHeader('X-Origin-Verify', [originVerifyValue])],
      action: elbv2.ListenerAction.forward([targetGroup]),
    });

    // -----------------------------------------------------------------------
    // CloudFront — 캐시 없음(전부 동적), POST 허용(/api/brief)
    // -----------------------------------------------------------------------
    const distribution = new cloudfront.Distribution(this, 'Cdn', {
      comment: 'Space Watch',
      defaultBehavior: {
        origin: new origins.HttpOrigin(alb.loadBalancerDnsName, {
          protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
          httpPort: 80,
          customHeaders: { 'X-Origin-Verify': originVerifyValue },
          readTimeout: Duration.seconds(60),   // 브리핑 Bedrock 호출이 12초대다
          keepaliveTimeout: Duration.seconds(60),
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,   // POST /api/brief
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        compress: true,
      },
      priceClass: cloudfront.PriceClass.PRICE_CLASS_200,
    });

    new CfnOutput(this, 'Url', { value: `https://${distribution.distributionDomainName}`, description: 'Public entry point' });
    new CfnOutput(this, 'AlbDnsName', { value: alb.loadBalancerDnsName, description: 'Direct access must return 403' });
    new CfnOutput(this, 'SecretName', { value: SECRET_NAME, description: 'Secret to populate with the Bearer token' });
    new CfnOutput(this, 'ClusterName', { value: cluster.clusterName });
    new CfnOutput(this, 'ServiceName', { value: service.serviceName });
  }
}
