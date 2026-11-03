"""Read-only semantic selection of trusted session artifacts."""
import json
from src.agent_runtime.task_artifacts import select, observe


class TaskResultsTool:
    async def execute(self, content, ctx):
        from src.agent_runtime.authority import ExactOperation
        from src.agent_runtime.audit_receipts import AuditReceiptError, lookup, remember_page
        from src.agent_runtime.resource_binding import resolve_filesystem_operation
        from src.agent_tools.vault_batch_tools import audit_report_page, validate_audit_arguments
        from src.agent_runtime.audit_followups import readable_report
        authority = ctx.get('request_authority')
        try:
            args = json.loads(content)
            if not isinstance(args, dict) or set(args) - {'kind', 'query', 'classification', 'offset'}:
                raise ValueError('Use kind/query to select a saved task, classification and offset for details; do not supply identifiers or paths')
            for key in ('kind', 'query'):
                if key in args and (not isinstance(args[key], str) or len(args[key]) > 200):
                    raise ValueError('Task selectors must be strings of at most 200 characters')
            record = select(authority.owner, authority.session_id, authority.workspace,
                            kind=args.get('kind'), query=args.get('query'))
            if record is None:
                raise AuditReceiptError('task_result_missing', 'No unique accessible saved task matches. Ask which completed task the user means.')
            if record['resource'] is not None:
                record['resource'].validate()
                if record['resource'].root not in authority.resource_roots:
                    raise ValueError('Task resource is outside the current sealed workspace scope')
            for source_tool in record.get('required_tools',()):
                if authority.restricted(ExactOperation.normalize(source_tool,'{}')):
                    raise ValueError('Saved evidence is denied by current source-tool policy')
            if record['kind'] != 'markdown_property_audit':
                result = {'kind':record['kind'], 'title':record['title'], 'result':record['result']}
                observe({'kind':record['kind'], 'fallback':json.dumps(result, ensure_ascii=False)})
                return {'output':json.dumps(result, ensure_ascii=False), 'exit_code':0}
            if authority.restricted(ExactOperation.normalize('vault_audit', '{}')):
                return {'error':'Audit evidence is denied by current policy', 'exit_code':1,
                        'blocked':True, 'failure_kind':'user_policy_denied'}
            ref = record['result']
            path, pattern, key, controls = ref['selection']
            operation_args = dict(path=path, glob=pattern, property=key, include_control_dirs=controls,
                audit_id=ref['audit_id'], report_offset=args.get('offset', 0), require_current=True)
            if 'classification' in args:
                operation_args['classification'] = args['classification']
            validate_audit_arguments(operation_args)
            # Binding validates confinement and the original directory identity.
            # This operation reads only cached results; it cannot execute a scan.
            operation = ExactOperation.normalize('vault_audit', json.dumps(operation_args))
            bound = resolve_filesystem_operation(operation, roots=authority.resource_roots, workspace=authority.workspace,
                request_id=authority.request_id, authority=authority)
            report = lookup({**ctx, 'resource_operation':bound}, path, operation_args)
            if report is None:
                raise AuditReceiptError('audit_receipt_missing', 'The task snapshot is unavailable; a new scoped audit requires authorization')
            page = audit_report_page(report, operation_args['report_offset'], detail_budget=1500, classification=args.get('classification'))
            from pathlib import Path
            relative = Path(path).relative_to(Path(authority.workspace).resolve()).as_posix()
            scope = '/workspace' if relative == '.' else '/workspace/' + relative
            readable = readable_report(report, page, args.get('classification'), scope)
            remember_page(authority, report['audit_id'], args.get('classification'), page['next_offset'])
            observe({'kind':record['kind'], 'fallback':readable})
            return {'output':json.dumps({'task_kind':record['kind'], 'title':record['title'], 'scope':scope, **page}, ensure_ascii=False), 'exit_code':0}
        except AuditReceiptError as error:
            return {'error':str(error), 'exit_code':1, 'failure_kind':error.code, 'executed':False}
        except (ValueError, OSError, TypeError) as error:
            return {'error':str(error), 'exit_code':1, 'failure_kind':'task_result_denied', 'blocked':True, 'executed':False}
