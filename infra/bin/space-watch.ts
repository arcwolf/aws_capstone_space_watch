#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { SpaceWatchStack } from '../lib/space-watch-stack';

const app = new cdk.App();

// account/region 을 명시한다 — 리전에 묶인 리소스(prefix list, ECR 자산)를 쓰므로
// 환경에 무관한(env-agnostic) 스택으로 두면 안 된다.
new SpaceWatchStack(app, 'SpaceWatch', {
  // 계정은 하드코딩하지 않는다 — cdk CLI 가 주변 자격증명에서 CDK_DEFAULT_ACCOUNT 를
  // 채워준다. 리전만 기본값을 둔다(Bedrock 모델 엔드포인트와 맞춰야 한다).
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? 'ap-northeast-2',
  },
  description: 'Space Watch - CloudFront -> ALB (prefix list + X-Origin-Verify) -> ECS Fargate',
});
