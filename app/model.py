"""LLM protocol and fixed structured responses for isolated tests."""
from .agents import RouteOutput, IntakeOutput, RiskOutput, CreditOutput, ReporterOutput, CollectionsOutput


class ModelUnavailable(RuntimeError):
    pass


class ModelBudgetExceeded(ModelUnavailable):
    pass


class FakeClient:
    """Explicitly injected test double; never selected from public requests or env."""
    def __init__(self):
        self.calls = []

    async def structured(self, agent, data, schema=None, *, deep=False, human_message=False):
        schema = schema or agent.output_schema
        self.calls.append((agent.name, schema.__name__))
        fixed = {
            RouteOutput: {"intent": "help", "reply": "I can help with applications, risk review, repayments and complaints. Which loan should we work on?"},
            IntakeOutput: {},
            RiskOutput: {"summary": "The attached report lists the deterministic identity and income checks. An officer must review the findings."},
            CreditOutput: {"summary": "The recommendation is based on the recorded risk checks and affordability calculation. A credit officer makes the final decision.", "reasons": ["Review the documented risk findings and affordability limits."]},
            ReporterOutput: {"summary": "Review complete. The officer can inspect the reports and make a decision."},
            CollectionsOutput: {"summary": "I am sorry to hear about your situation. We can send a restructure request to the credit officer for review."},
        }
        return schema.model_validate(fixed[schema])

    async def tool(self, agent, name, data, execute):
        agent.authorize(name)
        self.calls.append((agent.name, name))
        return execute()
