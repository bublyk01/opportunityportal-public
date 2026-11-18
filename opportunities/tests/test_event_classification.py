from django.test import SimpleTestCase

from opportunities.services.event_classification import classify_event_type


class EventClassificationTests(SimpleTestCase):
    def test_english_and_ukrainian_seminars_and_webinars_share_seminar_filter(self):
        for title in (
            "Research Seminar: Language Models", "Academic seminar", "Webinar: Career Planning",
            "Науковий семінар про тестові дані", "Вебінар про навчальні вправи",
        ):
            with self.subTest(title=title):
                self.assertEqual(classify_event_type(title), "seminar")

    def test_lectures_have_their_own_format(self):
        for title in ("Guest Lecture: Sample Data", "Гостьова лекція про навчальні приклади"):
            with self.subTest(title=title):
                self.assertEqual(classify_event_type(title), "lecture")

    def test_workshop_variants_include_normalized_combining_ukrainian_letters(self):
        for title in (
            "Creative Thinking Workshop", "Воркшоп з аналізу даних", "Майстер-клас з фотографії",
            "Маи\u0306стер-клас з фотографії", "Майстер‑клас з фотографії",
        ):
            with self.subTest(title=title):
                self.assertEqual(classify_event_type(title), "workshop")

    def test_conferences_are_separate_from_other_formats(self):
        self.assertEqual(classify_event_type("Example Technology Conference 2026"), "conference")
        self.assertEqual(classify_event_type("Наукова конференція для студентів"), "conference")

    def test_competitions_include_contests_tournaments_and_hackathons(self):
        for title in (
            "Student Research Competition", "Meme Contest", "Chess Tournament", "Student Hackathon",
            "Game Jam 2026", "Конкурс студентських проєктів", "Турнір з шахів",
            "Змагання з інтегрування", "Хакатон громадських технологій", "Студентський геймджем",
        ):
            with self.subTest(title=title):
                self.assertEqual(classify_event_type(title), "competition")

    def test_unambiguous_title_outweighs_different_formats_in_body(self):
        self.assertEqual(classify_event_type(
            "Example Technology Conference", "The program includes a lecture and several workshops.",
        ), "conference")

    def test_mixed_title_stays_blank_without_selecting_one_body_format(self):
        self.assertEqual(classify_event_type(
            "Гостьова лекція і воркшоп", "Запрошуємо на гостьову лекцію про ML.",
        ), "")
        self.assertEqual(classify_event_type("Conference and Workshop"), "")

    def test_description_can_supply_an_explicit_invitation_for_an_untyped_title(self):
        self.assertEqual(classify_event_type(
            "AI and Society", "Привіт усім!\nУніверситет запрошує на відкритий науковий семінар про ШІ.",
        ), "seminar")
        self.assertEqual(classify_event_type(
            "Thinking in Pictures", "Join our photography workshop at 18:00.",
        ), "workshop")

    def test_description_can_supply_an_explicit_format_label(self):
        self.assertEqual(classify_event_type("Understanding Economic Change", "Формат події: лекція\nОнлайн"), "lecture")

    def test_ambiguous_introduction_stays_blank(self):
        self.assertEqual(classify_event_type(
            "Learning Day", "Join a lecture and workshop about machine learning.",
        ), "")

    def test_late_body_mentions_cannot_supply_a_format(self):
        text = "\n".join(("Welcome", "Meet our speakers", "Bring your questions", "Doors open at 17:00", "More details soon", "Join a workshop"))
        self.assertEqual(classify_event_type("Community Evening", text), "")

    def test_jobs_and_incidental_benefits_are_not_classified_from_event_words(self):
        self.assertEqual(classify_event_type("Vacancy: Conference Coordinator", "Join our conference team."), "")
        self.assertEqual(classify_event_type("Junior Analyst", "Benefits: attending conferences and workshops."), "")
        self.assertEqual(classify_event_type("Research Assistant", "You can attend a research seminar each month."), "")

    def test_statistical_training_and_unknown_event_formats_stay_blank(self):
        for title, description in (
            ("Model Training", "We will train a statistical model using workshop participant data."),
            ("Example Community Conversation", "Join us for a conversation with a game developer."),
            ("City Simulation", "Take part in a collaborative city game."),
            ("", ""),
        ):
            with self.subTest(title=title):
                self.assertEqual(classify_event_type(title, description), "")

    def test_conference_about_competition_policy_is_not_two_event_formats(self):
        self.assertEqual(classify_event_type("Conference on Competition Policy"), "conference")

    def test_regular_announcement_heading_can_supply_format_without_invitation_word(self):
        self.assertEqual(classify_event_type("Example Math Challenge", "Змагання з інтегрування для студентів."), "competition")

    def test_volunteer_role_title_does_not_borrow_format_of_its_venue(self):
        for title in (
            "Volunteer at Example Conference 2026", "Volunteer for a Sample Event", "Volunteer with Example Academy",
            "Volunteers wanted at the conference", "Volunteering opportunity: Event Support",
            "Шукаємо волонтерів на конференцію", "Волонтери на турнір", "Volunteer", "Волонтер",
        ):
            with self.subTest(title=title):
                self.assertEqual(classify_event_type(title, "Join the international conference hosted in Example City."), "")

    def test_events_about_volunteering_still_receive_their_actual_format(self):
        self.assertEqual(classify_event_type(
            "Історія волонтера", "Запрошуємо на гостьову лекцію про досвід волонтерства.",
        ), "lecture")
        self.assertEqual(classify_event_type("Workshop for Volunteers"), "workshop")

    def test_clear_internship_role_is_not_its_optional_weekly_seminar(self):
        self.assertEqual(classify_event_type(
            "Paid Internship Opportunity: Weekly Seminar Assistant", "Join our research seminar as a team member.",
        ), "")
        self.assertEqual(classify_event_type("Weekly Seminar: Internship Experiences"), "seminar")
        self.assertEqual(classify_event_type("Internship Opportunities Webinar"), "seminar")
