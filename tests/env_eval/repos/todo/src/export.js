// The task list, exported for other apps. A task is { title, done }.

// CSV with a header row; a title with a comma or a quote is quoted, its quotes doubled.
export function toCsv(tasks) {
  throw new Error("toCsv is not written yet");
}

// JSON: { "count": <tasks>, "done": <tasks done>, "tasks": [{ "title", "done" }] }, two-space indent.
export function toJson(tasks) {
  throw new Error("toJson is not written yet");
}
