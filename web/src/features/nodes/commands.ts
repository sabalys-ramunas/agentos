/**
 * The command bar's environments. The shell lists every node itself (components/shell); this adds each
 * environment by its title, the one name a person knows it by, found by its title, repo and branch too.
 * A rented VM no environment owns is listed under its lane, as it opens beside Machines.
 */
import { Cloud } from 'lucide-react';
import { useAccess, useEnvironments, useVms } from '@/api/hooks';
import type { CommandContext, CommandSource } from '@/components/shell/commands';
import { ENV_STATUS } from '@/features/environments/words';
import { timeLeft } from './model';

export const commands: CommandSource = {
  id: 'environments',
  heading: 'Environments',
  order: 55,
  useItems: (org) => {
    const owner = useAccess(org).role === 'owner';
    const envs = useEnvironments(org, 'all').data?.environments ?? [];
    const vms = useVms(org, owner).data;
    const owned = new Set(envs.map((env) => env.lanes.terminal));
    const fromEnvs = envs.map((env) => ({
      id: `env-${env.id}`,
      label: env.title,
      detail: ENV_STATUS[env.state].label,
      icon: Cloud,
      keywords: `environment env vm machine ${env.title} ${env.repo ?? ''} ${env.branch ?? ''} ${env.id}`,
      onSelect: ({ navigate }: CommandContext) =>
        void navigate({ to: '/$org/environments/$id', params: { org, id: env.id } }),
    }));
    const loose = !vms?.available
      ? []
      : vms.vms
          .filter((vm) => vm.state !== 'deleted' && !owned.has(vm.lane))
          .map((vm) => ({
            id: vm.id,
            label: vm.lane,
            detail: timeLeft(vm) ?? vm.id,
            icon: Cloud,
            keywords: `environment vm machine ${vm.id} ${vm.server_type} ${vm.location}`,
            onSelect: ({ navigate }: CommandContext) =>
              void navigate({ to: '/$org/nodes/$id', params: { org, id: vm.id } }),
          }));
    return [...fromEnvs, ...loose];
  },
};
