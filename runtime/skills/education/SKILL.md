---
metadata_schema: grounded.skill.v2
description: Education, courses, lessons, homework, and progress workflow pack.
whenToUse:
  - обучение
  - курс
  - курсы
  - урок
  - уроки
  - домашнее задание
  - education
  - course
  - lesson
  - homework
  - learning
  - progress
paths:
  - miniapp/app/static/**
  - miniapp/app/routes/**
  - miniapp/tests/**
allowedTools:
  - read_files
  - search_files
  - apply_patch_to_draft
  - write_file
  - run_checks
  - browser_verify
model: default
effort: high
triggerRules:
  - Match prompts using this skill domain and path scope.
validation:
  - education_workflow
  - persisted_workflow
  - role_coverage
  - browser_flow_smoke
requiredProof:
  - Final readiness proof covers this skill.
incompatibleSkills:
  - ""
outputExpectations:
  - Produce working product changes and cite proof artifacts.
---
# Education

Use this skill when the product handles courses, cohorts, lessons, homework, tests, mentoring, or student progress.

## Rules

- Persist course, module, lesson, student enrollment, progress, assignment, submission, feedback, and deadline.
- Expose a client/student flow: enroll or open course, continue lesson, submit homework or mark progress, see feedback.
- Expose a specialist/teacher flow: review submissions, give feedback, update lesson availability, and track students.
- Expose a manager flow: cohort progress, overdue work, completion rate, revenue or enrollments, and risky students.
- Keep learning UI action-oriented: next lesson, due task, feedback, and progress should be visible without hunting.
- Avoid static course pages that cannot prove enrollment or progress state.

## Acceptance

- API proof persists enrollment or progress and reads it back.
- Browser proof completes a lesson/progress action and verifies teacher or manager sees the saved change.
- Role coverage includes student progress, teacher review, and manager cohort view.
- Mobile proof validates lesson list, task form, and feedback display.
- Tests cover progress or submission persistence.
