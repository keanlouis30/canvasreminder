#!/usr/bin/env python3
"""
Canvas Deadline Reminder App
A CLI application that integrates with Canvas LMS API to send automated assignment reminders
with detailed information via Facebook Messenger.
"""

import requests
import schedule
import time
import json
import argparse
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
import logging
from dataclasses import dataclass
import sys
import os

# Third-party imports for notifications
try:
    import plyer  # For desktop notifications
except ImportError: 
    print("Warning: plyer not installed. Desktop notifications will be disabled.")
    plyer = None

CANVAS_API_TOKEN = os.getenv('CANVAS_API_TOKEN', "your-default-token")
CANVAS_BASE_URL = os.getenv('CANVAS_BASE_URL', "https://dlsu.instructure.com/api/v1")
FACEBOOK_PAGE_ACCESS_TOKEN = os.getenv('FACEBOOK_PAGE_ACCESS_TOKEN', "your-default-token")
FACEBOOK_PAGE_ID = os.getenv('FACEBOOK_PAGE_ID', "your-default-page-id")
FACEBOOK_RECIPIENT_ID = os.getenv('FACEBOOK_RECIPIENT_ID', "your-default-recipient-id")
# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('canvas_reminder.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class Assignment:
    """Data class for Canvas assignments"""
    id: int
    name: str
    due_at: Optional[str]
    course_id: int
    course_name: str
    html_url: str
    points_possible: Optional[float]
    description: Optional[str] = None
    submission_types: Optional[List[str]] = None
    
    @property
    def due_datetime(self) -> Optional[datetime]:
        """Convert due_at string to datetime object"""
        if not self.due_at:
            return None
        try:
            # Canvas API returns dates in ISO format
            return datetime.fromisoformat(self.due_at.replace('Z', '+00:00'))
        except ValueError:
            logger.error(f"Failed to parse due date: {self.due_at}")
            return None
    
    @property
    def is_due_soon(self) -> bool:
        """Check if assignment is due within the next 7 days"""
        if not self.due_datetime:
            return False
        return self.due_datetime <= datetime.now(timezone.utc) + timedelta(days=7)
    
    @property
    def urgency_level(self) -> str:
        """Get urgency level based on due date"""
        if not self.due_datetime:
            return "no_date"
        
        now = datetime.now(timezone.utc)
        time_diff = self.due_datetime - now
        hours_until_due = time_diff.total_seconds() / 3600
        
        if hours_until_due < 0:
            return "overdue"
        elif hours_until_due < 1:
            return "critical"
        elif hours_until_due < 6:
            return "urgent"
        elif hours_until_due < 24:
            return "today"
        elif hours_until_due < 48:
            return "tomorrow"
        elif hours_until_due < 168:  # 7 days
            return "this_week"
        else:
            return "upcoming"


class CanvasAPI:
    """Canvas LMS API client"""
    
    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url
        self.headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json'
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
    
    def get_courses(self) -> List[Dict]:
        """Fetch all active courses for the user"""
        try:
            url = f"{self.base_url}/courses"
            params = {
                'enrollment_state': 'active',
                'per_page': 100
            }
            response = self.session.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch courses: {e}")
            return []
    
    def get_assignments(self, course_id: int) -> List[Dict]:
        """Fetch assignments for a specific course"""
        try:
            url = f"{self.base_url}/courses/{course_id}/assignments"
            params = {
                'per_page': 100,
                'order_by': 'due_at'
            }
            response = self.session.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch assignments for course {course_id}: {e}")
            return []
    
    def get_all_upcoming_assignments(self) -> List[Assignment]:
        """Fetch all upcoming assignments from all courses"""
        assignments = []
        courses = self.get_courses()
        
        for course in courses:
            course_assignments = self.get_assignments(course['id'])
            
            for assignment_data in course_assignments:
                # Skip assignments without due dates or already passed
                if not assignment_data.get('due_at'):
                    continue
                
                assignment = Assignment(
                    id=assignment_data['id'],
                    name=assignment_data['name'],
                    due_at=assignment_data['due_at'],
                    course_id=course['id'],
                    course_name=course['name'],
                    html_url=assignment_data['html_url'],
                    points_possible=assignment_data.get('points_possible'),
                    description=assignment_data.get('description'),
                    submission_types=assignment_data.get('submission_types', [])
                )
                
                # Only include assignments due in the future
                if assignment.due_datetime and assignment.due_datetime > datetime.now(timezone.utc):
                    assignments.append(assignment)
        
        # Sort by due date
        assignments.sort(key=lambda x: x.due_datetime or datetime.max)
        return assignments


class FacebookMessengerService:
    """Service for sending messages via Facebook Messenger"""
    
    def __init__(self, page_access_token: str = None, recipient_id: str = None):
        self.page_access_token = page_access_token
        self.recipient_id = recipient_id
        self.graph_api_url = "https://graph.facebook.com/v18.0/me/messages"
        
        # Check if Facebook Messenger is configured
        self.is_configured = bool(page_access_token and recipient_id)
        if not self.is_configured:
            logger.warning("Facebook Messenger not configured. Messages will not be sent.")
    
    def send_text_message(self, message: str) -> bool:
        """Send a text message via Facebook Messenger"""
        if not self.is_configured:
            logger.warning("Facebook Messenger not configured")
            return False
        
        try:
            payload = {
                "recipient": {"id": self.recipient_id},
                "message": {"text": message},
                "messaging_type": "MESSAGE_TAG",
                "tag": "ACCOUNT_UPDATE"
            }
            
            params = {"access_token": self.page_access_token}
            
            response = requests.post(
                self.graph_api_url,
                params=params,
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            
            response.raise_for_status()
            result = response.json()
            
            if "message_id" in result:
                logger.info(f"Facebook message sent successfully: {result['message_id']}")
                return True
            else:
                logger.error(f"Facebook message failed: {result}")
                return False
                
        except requests.RequestException as e:
            logger.error(f"Failed to send Facebook message: {e}")
            return False
    
    def send_detailed_assignment_message(self, assignment: Assignment) -> bool:
        """Send a detailed message with comprehensive assignment information"""
        if not self.is_configured:
            logger.warning("Facebook Messenger not configured")
            return False
        
        # Format due date with urgency indicators
        due_str = "No due date"
        urgency_emoji = "📅"
        
        if assignment.due_datetime:
            local_time = assignment.due_datetime.astimezone()
            now = datetime.now(timezone.utc)
            time_diff = assignment.due_datetime - now
            hours_until_due = time_diff.total_seconds() / 3600
            days_until_due = time_diff.days
            
            if assignment.urgency_level == "critical":
                urgency_emoji = "🚨"
                due_str = f"DUE IN {int(hours_until_due * 60)} MINUTES!"
            elif assignment.urgency_level == "urgent":
                urgency_emoji = "⚠️"
                due_str = f"DUE IN {int(hours_until_due)} HOURS - {local_time.strftime('%H:%M')}"
            elif assignment.urgency_level == "today":
                urgency_emoji = "🔥"
                due_str = f"DUE TODAY at {local_time.strftime('%H:%M')}"
            elif assignment.urgency_level == "tomorrow":
                urgency_emoji = "⏰"
                due_str = f"DUE TOMORROW at {local_time.strftime('%H:%M')}"
            elif days_until_due <= 7:
                urgency_emoji = "📅"
                due_str = f"Due {local_time.strftime('%A, %B %d at %H:%M')} ({days_until_due} days)"
            else:
                urgency_emoji = "📅"
                due_str = f"Due {local_time.strftime('%A, %B %d at %H:%M')}"
        
        # Format points
        points_str = f"{assignment.points_possible} points" if assignment.points_possible else "No points specified"
        
        # Format submission types
        submission_info = ""
        if assignment.submission_types:
            submission_types = [s.replace('_', ' ').title() for s in assignment.submission_types]
            submission_info = f"📤 Submission: {', '.join(submission_types)}\n"
        
        # Create comprehensive message
        message = (
            f"{urgency_emoji} *CANVAS ASSIGNMENT DETAILS*\n"
            f"{'='*40}\n\n"
            f"📝 ASSIGNMENT: {assignment.name}\n"
            f"🏫 COURSE: {assignment.course_name}\n"
            f"⏰ DUE: {due_str}\n"
            f"🎯 POINTS: {points_str}\n"
            f"{submission_info}"
            f"🔗 LINK: {assignment.html_url}\n\n"
            f"Assignment ID: {assignment.id}"
        )
        
        return self.send_text_message(message)


class NotificationService:
    """Service for sending notifications via multiple channels"""
    
    def __init__(self, facebook_token: str = None, facebook_recipient: str = None):
        self.facebook_service = FacebookMessengerService(facebook_token, facebook_recipient)
    
    def send_desktop_notification(self, title: str, message: str):
        """Send desktop notification"""
        if plyer:
            try:
                plyer.notification.notify(
                    title=title,
                    message=message,
                    timeout=10
                )
                logger.info(f"Desktop notification sent: {title}")
            except Exception as e:
                logger.error(f"Failed to send desktop notification: {e}")
        else:
            logger.warning("Desktop notifications disabled (plyer not installed)")
    
    def send_facebook_message(self, message: str):
        """Send Facebook Messenger message"""
        return self.facebook_service.send_text_message(message)
    
    def send_detailed_assignment_reminder(self, assignment: Assignment):
        """Send detailed reminder for a specific assignment"""
        return self.facebook_service.send_detailed_assignment_message(assignment)


class CanvasReminderApp:
    """Main application class"""
    
    def __init__(self):
        self.canvas_api = CanvasAPI(CANVAS_BASE_URL, CANVAS_API_TOKEN)
        self.notification_service = NotificationService(
            FACEBOOK_PAGE_ACCESS_TOKEN, FACEBOOK_RECIPIENT_ID
        )
        self.assignments_cache = []
        self.last_update = None
    
    def update_assignments(self):
        """Fetch and cache latest assignments"""
        logger.info("Updating assignments from Canvas...")
        self.assignments_cache = self.canvas_api.get_all_upcoming_assignments()
        self.last_update = datetime.now()
        logger.info(f"Updated {len(self.assignments_cache)} assignments")
    
    def get_assignments_due_soon(self, hours: int = 24) -> List[Assignment]:
        """Get assignments due within specified hours"""
        cutoff = datetime.now(timezone.utc) + timedelta(hours=hours)
        return [
            assignment for assignment in self.assignments_cache
            if assignment.due_datetime and assignment.due_datetime <= cutoff
        ]
    
    def get_assignments_by_urgency(self) -> Dict[str, List[Assignment]]:
        """Group assignments by urgency level"""
        urgency_groups = {
            'critical': [],
            'urgent': [],
            'today': [],
            'tomorrow': [],
            'this_week': [],
            'upcoming': []
        }
        
        for assignment in self.assignments_cache:
            urgency_groups[assignment.urgency_level].append(assignment)
        
        return urgency_groups
    
    def format_assignment_summary(self, assignments: List[Assignment]) -> str:
        """Format assignments summary for Facebook message"""
        if not assignments:
            return "🎉 NO ASSIGNMENTS DUE SOON!\n\nYou're all caught up! Time to relax! 😊"
        
        # Group by urgency
        urgency_groups = {}
        for assignment in assignments:
            urgency = assignment.urgency_level
            if urgency not in urgency_groups:
                urgency_groups[urgency] = []
            urgency_groups[urgency].append(assignment)
        
        summary = f"📚 CANVAS ASSIGNMENTS SUMMARY\n{'='*35}\n\n"
        summary += f"Total upcoming assignments: {len(assignments)}\n\n"
        
        # Priority order for display
        priority_order = ['critical', 'urgent', 'today', 'tomorrow', 'this_week', 'upcoming']
        
        for urgency in priority_order:
            if urgency not in urgency_groups or not urgency_groups[urgency]:
                continue
            
            urgency_assignments = urgency_groups[urgency]
            
            if urgency == 'critical':
                summary += f"🚨 CRITICAL (Due < 1 hour): {len(urgency_assignments)}\n"
            elif urgency == 'urgent':
                summary += f"⚠️ URGENT (Due < 6 hours): {len(urgency_assignments)}\n"
            elif urgency == 'today':
                summary += f"🔥 DUE TODAY: {len(urgency_assignments)}\n"
            elif urgency == 'tomorrow':
                summary += f"⏰ DUE TOMORROW: {len(urgency_assignments)}\n"
            elif urgency == 'this_week':
                summary += f"📅 DUE THIS WEEK: {len(urgency_assignments)}\n"
            elif urgency == 'upcoming':
                summary += f"📋 UPCOMING: {len(urgency_assignments)}\n"
            
            for assignment in urgency_assignments:
                due_str = "No due date"
                if assignment.due_datetime:
                    local_time = assignment.due_datetime.astimezone()
                    if urgency in ['critical', 'urgent']:
                        hours_until = (assignment.due_datetime - datetime.now(timezone.utc)).total_seconds() / 3600
                        due_str = f"{hours_until:.1f}h"
                    elif urgency == 'today':
                        due_str = local_time.strftime('%H:%M')
                    elif urgency == 'tomorrow':
                        due_str = local_time.strftime('%H:%M')
                    else:
                        due_str = local_time.strftime('%m/%d %H:%M')
                
                points = f"{assignment.points_possible}pts" if assignment.points_possible else "No pts"
                summary += f"  • {assignment.name[:40]}{'...' if len(assignment.name) > 40 else ''}\n"
                summary += f"    📖 {assignment.course_name[:30]}{'...' if len(assignment.course_name) > 30 else ''}\n"
                summary += f"    ⏰ {due_str} | 🎯 {points}\n\n"
        
        summary += f"Use 'list' command to see full details and links."
        return summary
    
    def format_all_assignments_list(self) -> str:
        """Format all assignments in a detailed list for Facebook message"""
        if not self.assignments_cache:
            return "📚 NO UPCOMING ASSIGNMENTS\n\nYou're all caught up! 🎉"
        
        # Group by urgency
        urgency_groups = self.get_assignments_by_urgency()
        
        message = f"📚 ALL UPCOMING ASSIGNMENTS\n{'='*40}\n\n"
        message += f"Total: {len(self.assignments_cache)} assignments\n\n"
        
        # Priority order for display
        priority_order = ['critical', 'urgent', 'today', 'tomorrow', 'this_week', 'upcoming']
        
        for urgency in priority_order:
            assignments = urgency_groups[urgency]
            if not assignments:
                continue
            
            # Section headers
            if urgency == 'critical':
                message += f"🚨 CRITICAL - DUE < 1 HOUR ({len(assignments)})\n"
            elif urgency == 'urgent':
                message += f"⚠️ URGENT - DUE < 6 HOURS ({len(assignments)})\n"
            elif urgency == 'today':
                message += f"🔥 DUE TODAY ({len(assignments)})\n"
            elif urgency == 'tomorrow':
                message += f"⏰ DUE TOMORROW ({len(assignments)})\n"
            elif urgency == 'this_week':
                message += f"📅 DUE THIS WEEK ({len(assignments)})\n"
            elif urgency == 'upcoming':
                message += f"📋 UPCOMING ({len(assignments)})\n"
            
            message += f"{'─'*40}\n"
            
            for i, assignment in enumerate(assignments, 1):
                # Format due date
                due_str = "No due date"
                if assignment.due_datetime:
                    local_time = assignment.due_datetime.astimezone()
                    time_diff = assignment.due_datetime - datetime.now(timezone.utc)
                    
                    if urgency in ['critical', 'urgent']:
                        hours_until = time_diff.total_seconds() / 3600
                        if hours_until < 1:
                            due_str = f"{int(hours_until * 60)}min - {local_time.strftime('%H:%M')}"
                        else:
                            due_str = f"{hours_until:.1f}h - {local_time.strftime('%H:%M')}"
                    elif urgency in ['today', 'tomorrow']:
                        due_str = local_time.strftime('%H:%M')
                    else:
                        due_str = local_time.strftime('%m/%d %H:%M')
                
                # Format points
                points_str = f"{assignment.points_possible}pts" if assignment.points_possible else "No pts"
                
                # Assignment entry
                assignment_name = assignment.name[:35] + "..." if len(assignment.name) > 35 else assignment.name
                course_name = assignment.course_name[:25] + "..." if len(assignment.course_name) > 25 else assignment.course_name
                
                message += f"{i}. {assignment_name}\n"
                message += f"   📖 {course_name}\n"
                message += f"   ⏰ {due_str} | 🎯 {points_str}\n"
                message += f"   🔗 {assignment.html_url}\n\n"
            
            message += "\n"
        
        return message
    
    def send_scheduled_reminders(self):
        """Send scheduled reminders"""
        logger.info("Sending scheduled reminders...")
        
        # Update assignments first
        self.update_assignments()
        
        # Get assignments due in next 24 hours
        due_soon = self.get_assignments_due_soon(24)
        
        # Send summary to Facebook
        facebook_summary = self.format_assignment_summary(due_soon)
        self.notification_service.send_facebook_message(facebook_summary)
        
        # Send desktop notification
        if due_soon:
            desktop_message = f"{len(due_soon)} assignment{'s' if len(due_soon) != 1 else ''} due soon"
            self.notification_service.send_desktop_notification(
                f"Canvas Daily Reminder", 
                desktop_message
            )
        else:
            self.notification_service.send_desktop_notification(
                "Canvas Daily Reminder", 
                "No assignments due soon! 🎉"
            )
    
    def send_detailed_reminders(self):
        """Send detailed individual reminders for urgent assignments"""
        logger.info("Sending detailed reminders for urgent assignments...")
        
        # Get urgent assignments (due within 6 hours)
        urgent = self.get_assignments_due_soon(6)
        
        for assignment in urgent:
            logger.info(f"Sending detailed reminder for: {assignment.name}")
            self.notification_service.send_detailed_assignment_reminder(assignment)
    
    def send_hourly_reminders(self):
        """Send reminders for assignments due within 1 hour"""
        logger.info("Checking for assignments due within 1 hour...")
        
        due_within_hour = self.get_assignments_due_soon(1)
        
        for assignment in due_within_hour:
            logger.info(f"Sending final reminder for: {assignment.name}")
            self.notification_service.send_detailed_assignment_reminder(assignment)
    
    def schedule_reminders(self):
        """Set up the reminder schedule"""
        # Daily summary reminders
        schedule.every().day.at("06:00").do(self.send_scheduled_reminders)
        schedule.every().day.at("08:00").do(self.send_scheduled_reminders)
        schedule.every().day.at("12:00").do(self.send_scheduled_reminders)
        schedule.every().day.at("16:00").do(self.send_scheduled_reminders)
        schedule.every().day.at("20:00").do(self.send_scheduled_reminders)
        
        # Detailed reminders for urgent assignments
        schedule.every().day.at("07:00").do(self.send_detailed_reminders)
        schedule.every().day.at("11:00").do(self.send_detailed_reminders)
        schedule.every().day.at("15:00").do(self.send_detailed_reminders)
        schedule.every().day.at("19:00").do(self.send_detailed_reminders)
        
        # Hourly check for critical reminders
        schedule.every().hour.do(self.send_hourly_reminders)
        
        # Update assignments every 2 hours
        schedule.every(2).hours.do(self.update_assignments)
        
        logger.info("Reminder schedule configured")
    
    def run_once(self):
        """Run a single check and send reminders"""
        logger.info("Running one-time reminder check...")
        self.send_scheduled_reminders()
        self.send_detailed_reminders()
    
    def list_assignments(self):
        """List all upcoming assignments with detailed information"""
        print("\n" + "="*80)
        print("DETAILED CANVAS ASSIGNMENTS")
        print("="*80)
        
        self.update_assignments()
        
        if not self.assignments_cache:
            print("No upcoming assignments found.")
            return
        
        # Group by urgency
        urgency_groups = self.get_assignments_by_urgency()
        
        for urgency in ['critical', 'urgent', 'today', 'tomorrow', 'this_week', 'upcoming']:
            assignments = urgency_groups[urgency]
            if not assignments:
                continue
            
            print(f"\n{'='*80}")
            if urgency == 'critical':
                print("🚨 CRITICAL - DUE WITHIN 1 HOUR")
            elif urgency == 'urgent':
                print("⚠️ URGENT - DUE WITHIN 6 HOURS")
            elif urgency == 'today':
                print("🔥 DUE TODAY")
            elif urgency == 'tomorrow':
                print("⏰ DUE TOMORROW")
            elif urgency == 'this_week':
                print("📅 DUE THIS WEEK")
            elif urgency == 'upcoming':
                print("📋 UPCOMING ASSIGNMENTS")
            print("="*80)
            
            for i, assignment in enumerate(assignments, 1):
                due_str = "No due date"
                if assignment.due_datetime:
                    local_time = assignment.due_datetime.astimezone()
                    due_str = local_time.strftime("%A, %B %d, %Y at %H:%M")
                    
                    # Add time remaining
                    time_diff = assignment.due_datetime - datetime.now(timezone.utc)
                    if time_diff.total_seconds() > 0:
                        hours_remaining = time_diff.total_seconds() / 3600
                        if hours_remaining < 1:
                            due_str += f" ({int(hours_remaining * 60)} minutes remaining)"
                        elif hours_remaining < 24:
                            due_str += f" ({hours_remaining:.1f} hours remaining)"
                        else:
                            due_str += f" ({time_diff.days} days remaining)"
                
                points_str = f"{assignment.points_possible} points" if assignment.points_possible else "No points specified"
                
                submission_str = ""
                if assignment.submission_types:
                    submission_types = [s.replace('_', ' ').title() for s in assignment.submission_types]
                    submission_str = f"   Submission Types: {', '.join(submission_types)}"
                
                print(f"\n{i}. {assignment.name}")
                print(f"   Course: {assignment.course_name}")
                print(f"   Due: {due_str}")
                print(f"   Points: {points_str}")
                if submission_str:
                    print(submission_str)
                print(f"   URL: {assignment.html_url}")
                print(f"   Assignment ID: {assignment.id}")
        
        print(f"\n{'='*80}")
        print(f"Total assignments: {len(self.assignments_cache)}")
    
    def send_details_for_assignment(self, assignment_name: str = None):
        """Send detailed Facebook message for a specific assignment"""
        if not assignment_name:
            # Send details for all assignments due soon
            due_soon = self.get_assignments_due_soon(24)
            if not due_soon:
                self.notification_service.send_facebook_message("No assignments due soon!")
                return
            
            for assignment in due_soon:
                self.notification_service.send_detailed_assignment_reminder(assignment)
        else:
            # Find specific assignment
            matching_assignments = [
                a for a in self.assignments_cache 
                if assignment_name.lower() in a.name.lower()
            ]
            
            if not matching_assignments:
                self.notification_service.send_facebook_message(f"No assignment found matching '{assignment_name}'")
                return
            
            for assignment in matching_assignments:
                self.notification_service.send_detailed_assignment_reminder(assignment)
    
    def run_daemon(self):
        """Run the app as a daemon process"""
        logger.info("Starting Canvas Reminder daemon...")
        
        # Initial setup
        self.update_assignments()
        self.schedule_reminders()
        
        # Send startup message
        startup_message = (
            f"🚀 CANVAS REMINDER STARTED!\n"
            f"{'='*35}\n\n"
            f"Now monitoring {len(self.assignments_cache)} upcoming assignments.\n\n"
            f"Scheduled reminders:\n"
            f"📅 Daily summaries: 6AM, 8AM, 12PM, 4PM, 8PM\n"
            f"⚠️ Detailed urgents: 7AM, 11AM, 3PM, 7PM\n"
            f"🚨 Hourly critical checks\n\n"
            f"Use 'once' command to get immediate update!"
        )
        
        self.notification_service.send_desktop_notification(
            "Canvas Reminder Started", 
            f"Monitoring {len(self.assignments_cache)} assignments"
        )
        self.notification_service.send_facebook_message(startup_message)
        
        # Send individual assignment details
        self.send_individual_assignment_details()
        
        # Main loop
        try:
            while True:
                schedule.run_pending()
                time.sleep(60)  # Check every minute
        except KeyboardInterrupt:
            logger.info("Shutting down Canvas Reminder daemon...")
            shutdown_message = "🛑 Canvas Reminder Stopped\n\nAssignment monitoring has been stopped."
            
            self.notification_service.send_desktop_notification(
                "Canvas Reminder Stopped", 
                shutdown_message
            )
            self.notification_service.send_facebook_message(shutdown_message)

    def send_individual_assignment_details(self):
        """Send individual detailed messages for all upcoming assignments"""
        if not self.assignments_cache:
            self.notification_service.send_facebook_message("📚 NO UPCOMING ASSIGNMENTS\n\nYou're all caught up! 🎉")
            return
        
        # Group by urgency and send assignments in priority order
        urgency_groups = self.get_assignments_by_urgency()
        priority_order = ['critical', 'urgent', 'today', 'tomorrow', 'this_week', 'upcoming']
        
        total_sent = 0
        for urgency in priority_order:
            assignments = urgency_groups[urgency]
            if not assignments:
                continue
            
            for assignment in assignments:
                self.notification_service.send_detailed_assignment_reminder(assignment)
                total_sent += 1
                # Add a small delay between messages to avoid rate limiting
                time.sleep(1)
        
        # Send final summary message
        summary_message = (
            f"📋 ASSIGNMENT LOADING COMPLETE\n"
            f"{'='*35}\n\n"
            f"✅ Sent details for {total_sent} assignments\n"
            f"📚 Total upcoming assignments: {len(self.assignments_cache)}\n\n"
            f"You should have received individual messages for each assignment above."
        )
        self.notification_service.send_facebook_message(summary_message)
def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(description="Canvas Deadline Reminder App with Detailed Facebook Messages")
    parser.add_argument(
        'command', 
        choices=['start', 'once', 'list', 'test', 'details'],
        help='Command to execute'
    )
    parser.add_argument(
        '--assignment', 
        help='Assignment name to get details for (use with details command)'
    )
    
    args = parser.parse_args()
    app = CanvasReminderApp()
    
    if args.command == 'start':
        print("Starting Canvas Reminder daemon...")
        print("Press Ctrl+C to stop")
        app.run_daemon()

    elif args.command == 'once':
        app.run_once()
    
    elif args.command == 'list':
        app.list_assignments()
    
    elif args.command == 'details':
        app.update_assignments()
        app.send_details_for_assignment(args.assignment)
        print("Detailed assignment information sent to Facebook Messenger!")
    
    elif args.command == 'test':
        print("Testing notifications...")
        app.notification_service.send_desktop_notification(
            "Test Notification", 
            "This is a test notification from Canvas Reminder"
        )
        app.notification_service.send_facebook_message(
            "🧪 TEST MESSAGE\n"
            "========================\n\n"
            "This confirms Facebook Messenger integration is working!\n\n"
            "✅ Connection successful\n"
            "✅ Message delivery confirmed\n"
            "✅ Ready for assignment reminders"
        )
        print("Test notifications sent!")


if __name__ == "__main__":
    main()