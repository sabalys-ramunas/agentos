/**
 * The command bar's rows from Machines. The shell lists every node itself (components/shell); this
 * adds the disposable VMs the owner rents, which open beside the table like any machine.
 */
import { Cloud } from 'lucide-react';
import { useAccess, useVms } from '@/api/hooks';
import type { CommandSource } from '@/components/shell/commands';
import { timeLeft } from './model';

export const commands: CommandSource = {
  id: 'vms',
  heading: 'Disposable VMs',
  order: 55,
  useItems: (org) => {
    const owner = useAccess(org).role === 'owner';
    const vms = useVms(org, owner).data;
    if (!vms?.available) return [];
    return vms.vms
      .filter((vm) => vm.state !== 'deleted')
      .map((vm) => ({
        id: vm.id,
        label: vm.lane,
        detail: timeLeft(vm) ?? vm.id,
        icon: Cloud,
        keywords: `vm machine ${vm.id} ${vm.server_type} ${vm.location}`,
        onSelect: ({ navigate }) => void navigate({ to: '/$org/nodes/$id', params: { org, id: vm.id } }),
      }));
  },
};
