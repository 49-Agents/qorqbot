"""Independent question controls, submitted through the existing Signoff transaction."""
from jsonschema import Draft202012Validator

from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError, canonical_bytes

TEXT = {"type": "string", "minLength": 1, "maxLength": 8000, "pattern": r"\S"}
IDENTIFIER = {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,40}$"}
CHOICE = schema({"id": IDENTIFIER,
    "label": {"type": "string", "maxLength": 80, "pattern": r"^\S+(?:\s+\S+)?$"},
    "explanation": TEXT}, ("id", "label", "explanation"))
QUESTION = schema({"id": IDENTIFIER, "prompt": {**TEXT, "maxLength": 200},
    "explanation": TEXT, "selection_mode": {"enum": ["none", "single", "multiple"]},
    "required": {"type": "boolean"}, "notes_required": {"type": "boolean"},
    "choices": {"type": "array", "items": CHOICE, "maxItems": 26}},
    ("id", "prompt", "selection_mode"))
FORM = schema({"schema_version": {"type": "integer", "const": 2},
    "kind": {"const": "question"}, "title": {**TEXT, "maxLength": 200}, "explanation": TEXT,
    "response_policy": {"const": "owner_only"},
    "questions": {"type": "array", "items": QUESTION, "minItems": 1, "maxItems": 10}},
    ("schema_version", "kind", "title", "explanation", "questions"))
ANSWER = schema({"question_id": IDENTIFIER,
    "selected_choices": {"type": "array", "items": IDENTIFIER, "uniqueItems": True, "maxItems": 26},
    "notes": {"type": "string", "maxLength": 8000}}, ("question_id", "selected_choices", "notes"))
_FORM_VALIDATOR, _ANSWER_VALIDATOR = Draft202012Validator(FORM), Draft202012Validator(ANSWER)
MAX_BYTES = 65536


def is_questionnaire(form):
    return isinstance(form, dict) and form.get('schema_version') == 2


def validate_form(form):
    if not _FORM_VALIDATOR.is_valid(form):
        raise DomainError('validation_error', 'Invalid version-2 question form')
    result = {**form, 'response_policy': 'owner_only', 'questions': []}
    ids = set()
    for value in form['questions']:
        question = {'required': True, 'notes_required': False, 'choices': [], **value}
        if question['id'] in ids:
            raise DomainError('validation_error', 'Question IDs must be unique')
        ids.add(question['id'])
        choices, mode = question['choices'], question['selection_mode']
        if len({choice['id'] for choice in choices}) != len(choices):
            raise DomainError('validation_error', 'Choice IDs must be unique within each question')
        if (mode == 'none' and choices) or (mode != 'none' and len(choices) < 2):
            raise DomainError('validation_error', 'Question selection mode and choices disagree')
        result['questions'].append(question)
    if len(canonical_bytes(result)) > MAX_BYTES:
        raise DomainError('validation_error', 'Question form must fit within 64 KiB')
    return result


def validate_answers(form, answers):
    """Return all answers in frozen question order, without any writes or text rewriting."""
    if not isinstance(answers, list) or len(answers) != len(form['questions']):
        raise DomainError('validation_error', 'Supply one answer for every question, including optional questions')
    by_id = {}
    for answer in answers:
        if not _ANSWER_VALIDATOR.is_valid(answer):
            raise DomainError('validation_error', 'Each answer needs a question ID, unique choice IDs and notes of at most 8000 characters')
        if answer['question_id'] in by_id:
            raise DomainError('validation_error', 'Question answers must not be repeated')
        by_id[answer['question_id']] = answer
    if set(by_id) != {q['id'] for q in form['questions']}:
        raise DomainError('validation_error', 'Answer only the questions in this Signoff')
    errors, result = {}, []
    for question in form['questions']:
        answer = by_id[question['id']]
        notes, selected = answer['notes'], answer['selected_choices']
        filled = bool(notes.strip() or selected)
        allowed = {choice['id'] for choice in question['choices']}
        if set(selected) - allowed or (question['selection_mode'] == 'single' and len(selected) > 1):
            errors[question['id']] = 'Select only choices belonging to this question.'
        elif question['required'] and not filled:
            errors[question['id']] = 'Choose an option or write an answer.'
        elif filled and question['notes_required'] and not notes.strip():
            errors[question['id']] = 'A written answer is required.'
        result.append({'question_id': question['id'], 'selected_choices': sorted(selected),
                       'notes': notes, 'status': 'answered' if filled else 'skipped'})
    if errors:
        raise DomainError('validation_error', 'Complete the required answers before submitting', details={'field_errors': errors})
    if sum(len(a['notes']) for a in result) > 32000 or len(canonical_bytes(result)) > MAX_BYTES:
        raise DomainError('validation_error', 'Combined answers must contain at most 32000 characters of notes and fit within 64 KiB')
    return result


def answer_messages(form, answers):
    """Bounded conversation projections; the structured decision remains authoritative."""
    messages = []
    for question, answer in zip(form['questions'], answers):
        lines = [question['prompt']]
        if answer['status'] == 'skipped':
            lines.append('Not answered (optional).')
        else:
            labels = [c['label'] for c in question['choices'] if c['id'] in answer['selected_choices']]
            if labels:
                lines.append('Selected: ' + ', '.join(labels))
            if answer['notes'].strip():
                lines.append(answer['notes'])
        messages.append('\n\n'.join(lines))
    return messages
