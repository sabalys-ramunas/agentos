/** `/machines`: every machine in one table (machines.tsx). */
import { useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { useState } from 'react';
import { Machines } from './machines';

export function Nodes() {
  const { org } = useParams({ from: '/$org/nodes' });
  const search = useSearch({ from: '/$org/nodes' });
  const navigate = useNavigate({ from: '/$org/nodes' });
  const [opened, setOpened] = useState(false);
  const table = { sort: search.sort, dir: search.dir };
  return (
    <Machines
      org={org}
      table={table}
      addOpen={opened || search.add === true}
      onAdd={(next) => {
        setOpened(next);
        if (!next && search.add) void navigate({ search: (previous) => ({ ...previous, add: undefined }), replace: true });
      }}
      onSelect={(id) => {
        if (id) void navigate({ to: '/$org/nodes/$id', params: { org, id }, search: table });
      }}
      onSort={(next) => void navigate({ search: (previous) => ({ ...previous, ...next }), replace: true, resetScroll: false })}
    />
  );
}
