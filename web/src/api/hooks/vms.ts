import { useQuery } from '@tanstack/react-query';
import { vms } from '../client';
import { keys } from '../keys';

export const useVms = (org: string, enabled = true) => useQuery({ queryKey: keys.vms(org), queryFn: () => vms.list(org), enabled });
