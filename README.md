# 📚 CourseHive – E-Learning Course Management System

CourseHive is a web-based e-learning platform designed to simplify course management and student enrollment. It provides a centralized platform where students can explore courses, view course descriptions, access instructor details, and enroll in their preferred courses through a user-friendly interface.

## ✨ Features

* 📖 **Course Management:** Add, view, and manage courses.
* 📝 **Course Details:** Display course names, descriptions, and instructor information.
* 👨‍🎓 **Student Enrollment:** Manage student enrollment in selected courses.
* 👤 **Admin Management:** Manage courses and administrative tasks.
* 🗄️ **Database Integration:** Store and manage course and enrollment information.
* 🎨 **User-Friendly Interface:** Simple navigation and an organized layout.

## 🛠️ Technologies Used

* **Frontend:** HTML, CSS, JavaScript
* **Backend:** Python Flask
* **Database:** MySQL
* **Development Environment:** Visual Studio Code
* **Version Control:** Git and GitHub

## 📂 Project Structure

```text
CourseHive/
├── app.py
├── db.py
├── create_admin.py
├── templates/
├── static/
├── requirements.txt
└── README.md
```

## ⚙️ Installation and Setup

### 1. Clone the Repository

```bash
git clone https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git
```

Replace `YOUR-USERNAME` and `YOUR-REPOSITORY` with your actual GitHub username and repository name.

### 2. Navigate to the Project Directory

```bash
cd CourseHive
```

### 3. Create a Virtual Environment

```bash
python -m venv venv
```

Activate it on Windows:

```bash
venv\Scripts\activate
```

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

### 5. Configure the Database

* Install and start MySQL.
* Create the database required by the application.
* Configure the database connection settings.
* Ensure the necessary tables are created.

**Note:** Keep your database credentials and passwords private. Never upload sensitive credentials to GitHub.

### 6. Run the Application

```bash
python app.py
```

Open your browser and visit:

```text
http://127.0.0.1:5000/
```

The application will be available at this address if Flask starts successfully on the default port.

## 🎯 Project Objective

The main objective of CourseHive is to provide an organized and accessible platform for managing courses and student enrollments. It aims to simplify course administration and make course information easier for students to access.

## 🚀 Future Enhancements

* Course search and filtering
* Student progress tracking
* Online learning resources
* Enrollment history and dashboards
* Course ratings and reviews
* Enhanced authentication and authorization
* Cloud deployment

## 🤝 Contribution

Suggestions and improvements are welcome. Developers can fork the repository, make changes, and submit pull requests.

## 📄 License

This project is developed
