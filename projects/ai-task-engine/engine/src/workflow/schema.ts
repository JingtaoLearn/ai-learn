import { z } from 'zod';

const AcceptanceSchema = z.discriminatedUnion('type', [
  z.object({
    type: z.literal('human_confirm'),
  }),
  z.object({
    type: z.literal('ai_self_check'),
    criteria: z.string().min(1, 'criteria required for ai_self_check'),
  }),
  z.object({
    type: z.literal('automated'),
    command: z.string().min(1, 'command required for automated'),
  }),
]);

const StepSchema = z.object({
  name: z.string().min(1),
  goal: z.string().min(1),
  background: z.string().optional(),
  rules: z.array(z.string()).optional().default([]),
  acceptance: AcceptanceSchema,
  timeout: z.string().optional(),
  wakePolicy: z.enum(['timeout', 'dependency', 'manual']).optional().default('dependency'),
  maxRetries: z.number().int().min(0).optional().default(0),
});

export const WorkflowSchema = z.object({
  name: z.string().min(1),
  description: z.string().optional(),
  steps: z.array(StepSchema).min(1, 'workflow must have at least one step'),
});

export type WorkflowDefinition = z.infer<typeof WorkflowSchema>;
export type StepDefinition = z.infer<typeof StepSchema>;
export type AcceptanceDefinition = z.infer<typeof AcceptanceSchema>;

export function parseTimeout(timeout: string): number | undefined {
  if (!timeout) return undefined;
  const match = timeout.match(/^(\d+)(s|m|h|d)$/);
  if (!match) return undefined;
  const value = parseInt(match[1], 10);
  const unit = match[2];
  const multipliers: Record<string, number> = { s: 1, m: 60, h: 3600, d: 86400 };
  return value * (multipliers[unit] ?? 1);
}
