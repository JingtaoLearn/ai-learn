import { StepExecutor, ExecutorInput, ExecutorResult } from './interface';

export class MockExecutor implements StepExecutor {
  name = 'mock';

  async execute(input: ExecutorInput): Promise<ExecutorResult> {
    console.log(`[mock-executor] Executing step: ${input.stepName} (task: ${input.taskId})`);
    console.log(`[mock-executor] Goal: ${input.goal}`);
    if (input.background) {
      console.log(`[mock-executor] Background: ${input.background.substring(0, 200)}...`);
    }
    if (input.rules.length > 0) {
      console.log(`[mock-executor] Rules: ${input.rules.join(', ')}`);
    }

    // Simulate async execution time
    await new Promise(resolve => setTimeout(resolve, 1000));

    const output: ExecutorResult = {
      success: true,
      output: {
        summary: `[MOCK] Completed step "${input.stepName}". Goal: ${input.goal}`,
        artifacts: [],
        metadata: {
          executor: 'mock',
          stepId: input.stepId,
          simulated: true,
        },
        completedAt: new Date().toISOString(),
      },
    };

    console.log(`[mock-executor] Step completed: ${input.stepName}`);
    return output;
  }
}
