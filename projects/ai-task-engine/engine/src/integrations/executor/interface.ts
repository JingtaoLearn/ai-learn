export interface ExecutorInput {
  stepId: string;
  taskId: string;
  stepIndex?: number;
  stepName: string;
  goal: string;
  background: string | null;
  rules: string[];
  acceptanceCriteria: string | null;
  discordChannelId: string | null;
  previousOutput: string | null;
  retryContext?: {
    retryCount: number;
    previousError: string | null;
    previousOutput: string | null;
  };
}

export interface ExecutorLoopConfig {
  maxIterations: number;
  evaluationDelay: number;   // ms to wait after trigger before polling
  pollInterval: number;      // ms between polls for response
  pollTimeout: number;       // ms max wait for a single response
}

export const DEFAULT_LOOP_CONFIG: ExecutorLoopConfig = {
  maxIterations: 10,
  evaluationDelay: 30_000,
  pollInterval: 5_000,
  pollTimeout: 5 * 60 * 1000,
};

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
