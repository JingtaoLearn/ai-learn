export interface ExecutorInput {
  stepId: string;
  taskId: string;
  stepIndex?: number;
  stepName: string;
  goal: string;
  background: string | null;
  rules: string[];
  discordChannelId: string | null;
  previousOutput: string | null;
  retryContext?: {
    retryCount: number;
    previousError: string | null;
    previousOutput: string | null;
  };
}

export interface ExecutorOutput {
  summary: string;
  artifacts: Array<{ type: 'file' | 'url'; path?: string; value?: string }>;
  metadata: Record<string, unknown>;
  completedAt: string;
}

export interface ExecutorResult {
  success: boolean;
  output?: ExecutorOutput;
  error?: string;
}

export interface StepExecutor {
  execute(input: ExecutorInput): Promise<ExecutorResult>;
  name: string;
}
