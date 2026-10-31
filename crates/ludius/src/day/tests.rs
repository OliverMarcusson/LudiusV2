use std::cell::{Cell, RefCell};
use std::path::Path;
use std::rc::Rc;

use super::*;

type Calls = Rc<RefCell<Vec<String>>>;

#[derive(Default)]
struct Fake {
    calls: Calls,
    locked_out: bool,
    dirty: bool,
    time: Option<Rc<Cell<NaiveDateTime>>>,
}

impl Desk for Fake {
    fn run(&mut self, task: &mut Task, between: Option<Between>, hygiene: bool, _: &BTreeSet<String>) -> Result<Outcome> {
        if hygiene && !self.normalize()? && self.escalate().is_err() {
            return Ok(Outcome { status: "abandoned".into(), steps: 0, reason: "escalation denied".into(), subgoals_done: vec![] });
        }
        if let Some(t) = &self.time {
            self.calls.borrow_mut().push(t.get().format("%H:%M").to_string());
        }
        self.calls.borrow_mut().push(format!("run {}", task.inst.params["file_name"]));
        if let Some(b) = between {
            b(self)?;
        }
        Ok(Outcome { status: "done".into(), steps: 1, reason: String::new(), subgoals_done: vec![] })
    }
    fn normalize(&mut self) -> Result<bool> {
        self.calls.borrow_mut().push("normalize".into());
        Ok(!self.dirty)
    }
    fn escalate(&mut self) -> Result<u32> {
        Err(EscalationDenied("LUDIUS_NO_ESCALATE=1".into()).into())
    }
    fn unlock(&mut self) -> Result<bool> {
        self.calls.borrow_mut().push("unlock".into());
        Ok(!self.locked_out)
    }
    fn lock(&mut self) -> Result<()> {
        self.calls.borrow_mut().push("lock".into());
        Ok(())
    }
}

fn note(at: &str, interrupt: bool) -> PlanItem {
    let params = [("file_name".to_string(), format!("note {}", at.replace(':', ""))), ("body".to_string(), "text".to_string())];
    PlanItem { template: "notepad_write_note".into(), at: at.into(), interrupt, params: params.into_iter().collect(), ..Default::default() }
}

fn brk(at: &str) -> PlanItem {
    PlanItem { template: BREAK.into(), at: at.into(), minutes: 30, ..Default::default() }
}

fn plan(items: Vec<PlanItem>) -> DayPlan {
    DayPlan { persona: "aberg".into(), date: "2026-11-02".into(), items }
}

fn log(name: &str) -> GroundTruth {
    // SAFETY: tests only read LUDIUS_DATA, always set to the same value.
    unsafe { std::env::set_var("LUDIUS_DATA", Path::new(env!("CARGO_MANIFEST_DIR")).join("../../controller/data")) };
    let path = std::env::temp_dir().join(format!("ludius-day-{}-{name}.jsonl", std::process::id()));
    let _ = std::fs::remove_file(&path);
    GroundTruth::open(&path).unwrap()
}

fn time_jump<'r>(log: &'r GroundTruth, calls: &Calls, clock_fails: bool) -> DayRunner<'r> {
    let mut day = DayRunner::new(crate::planner::templates().unwrap(), log, Clock::TimeJump, 0);
    let c = calls.clone();
    day.sleep = Box::new(move |s| c.borrow_mut().push(format!("sleep {s}")));
    let c = calls.clone();
    day.set_clock = Some(Box::new(move |t| {
        anyhow::ensure!(!clock_fails, "set-clock failed");
        c.borrow_mut().push(t.map_or("clock reset".into(), |t| format!("clock {}", t.format("%H:%M"))));
        Ok(())
    }));
    day
}

fn d() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 11, 2).unwrap()
}

#[test]
fn time_jump_day_keeps_order_breaks_and_interrupts() {
    let p = plan(vec![note("10:00", false), note("08:55", true), note("09:00", false), note("09:10", true), brk("12:00"), note("12:05", true)]);
    let got: Vec<(String, Vec<String>)> = slots(&p).into_iter().map(|(i, xs)| (i.at, xs.into_iter().map(|x| x.at).collect())).collect();
    let want: Vec<(String, Vec<String>)> = vec![("08:55".into(), vec![]), ("09:00".into(), vec!["09:10".into()]), ("10:00".into(), vec![]), ("12:00".into(), vec!["12:05".into()])];
    assert_eq!(got, want);

    let (log, calls) = (log("jump"), Calls::default());
    let mut desk = Fake { calls: calls.clone(), ..Default::default() };
    let mut day = time_jump(&log, &calls, false);
    let results = day.run(&mut desk, &p).unwrap();
    let want = "clock 08:55,normalize,run note 0855,clock 09:00,normalize,run note 0900,run note 0910,clock 10:00,normalize,run note 1000,\
                clock 12:00,normalize,lock,sleep 5,clock 12:30,unlock,normalize,run note 1205,clock reset";
    assert_eq!(calls.borrow().join(","), want);
    let got: Vec<(&str, &str)> = results.iter().map(|r| (r.item.at.as_str(), r.status.as_str())).collect();
    assert_eq!(got, [("08:55", "done"), ("09:10", "done"), ("09:00", "done"), ("10:00", "done"), ("12:00", "done"), ("12:05", "done")]);

    assert_eq!(day.slot(&mut desk, d(), &note("13:00", false), &mut vec![], None).unwrap()[0].item.at, "13:00");
    calls.borrow_mut().clear();
    day.slot(&mut desk, d(), &note("12:05", false), &mut vec![], Some("12:30")).unwrap();
    assert_eq!(calls.borrow()[..2], ["clock 12:30", "normalize"]);
    calls.borrow_mut().clear();
    // an interrupt writing the same file waits until the task is done
    let same = PlanItem { at: "09:10".into(), ..note("09:00", true) };
    day.slot(&mut desk, d(), &note("09:00", false), &mut vec![same], None).unwrap();
    assert_eq!(calls.borrow().iter().filter(|c| c.starts_with("run")).collect::<Vec<_>>(), ["run note 0900", "run note 0900"]);
    assert_eq!(day.results[day.results.len() - 2..].iter().map(|r| r.item.at.as_str()).collect::<Vec<_>>(), ["09:00", "09:10"]);
}

#[test]
fn failed_break_unlock_stops_the_day_and_resets_the_clock() {
    let (log, calls) = (log("unlock"), Calls::default());
    let mut desk = Fake { calls: calls.clone(), locked_out: true, ..Default::default() };
    let mut day = time_jump(&log, &calls, false);
    let e = day.run(&mut desk, &plan(vec![note("09:00", false), brk("12:00"), note("12:05", true), note("13:00", false)])).unwrap_err();
    assert!(e.is::<BreakUnlockFailed>() && e.to_string().contains("12:00"));
    assert_eq!(calls.borrow().join(","), "clock 09:00,normalize,run note 0900,clock 12:00,normalize,lock,sleep 5,clock 12:30,unlock,clock reset");
    assert_eq!(day.results.iter().map(|r| (r.item.at.as_str(), r.status.as_str())).collect::<Vec<_>>(), [("09:00", "done"), ("12:00", "failed")]);
    let events = std::fs::read_to_string(&log.path).unwrap();
    assert!(events.contains("\"status\":\"failed\"") && !events.contains("day_end"), "{events}");
}

#[test]
fn break_end_fails_closed_when_the_clock_cannot_be_set() {
    let (log, calls) = (log("clock"), Calls::default());
    let mut desk = Fake { calls: calls.clone(), ..Default::default() };
    let mut day = time_jump(&log, &calls, true);
    assert!(day.end_break(&mut desk, d(), &brk("12:00")).is_err(), "end_break acted on an unknown clock");
    assert!(!calls.borrow().contains(&"unlock".to_string()));
}

#[test]
fn realtime_day_waits_for_each_slot_in_logical_order() {
    let (log, calls) = (log("realtime"), Calls::default());
    let t = Rc::new(Cell::new(d().and_hms_opt(8, 0, 0).unwrap()));
    let mut desk = Fake { calls: calls.clone(), time: Some(t.clone()), ..Default::default() };
    let mut day = DayRunner::new(crate::planner::templates().unwrap(), &log, Clock::Realtime, 0);
    let t2 = t.clone();
    day.sleep = Box::new(move |s| t2.set(t2.get() + Duration::milliseconds((s * 1000.0) as i64)));
    day.now = Box::new(move || t.get());
    day.run(&mut desk, &plan(vec![note("09:00", false), note("08:55", true)])).unwrap();
    let runs: Vec<String> = calls.borrow().iter().filter(|c| *c != "normalize").cloned().collect();
    assert!(["08:55", "08:56", "08:57"].contains(&runs[0].as_str()) && runs[1] == "run note 0855", "{runs:?}");
    assert!(runs[2].as_str() >= "09:00" && runs[3] == "run note 0900", "{runs:?}");
}

#[test]
fn denied_escalation_abandons_the_task_and_the_day_goes_on() {
    let (log, calls) = (log("denied"), Calls::default());
    let mut desk = Fake { calls: calls.clone(), dirty: true, ..Default::default() };
    let mut day = DayRunner::new(crate::planner::templates().unwrap(), &log, Clock::Compressed, 0);
    let got: Vec<String> = day.run(&mut desk, &plan(vec![note("09:00", false), note("10:00", false)])).unwrap().into_iter().map(|r| r.status).collect();
    assert_eq!(got, ["abandoned", "abandoned"]);
    assert!(!calls.borrow().iter().any(|c| c.starts_with("run")));
}
